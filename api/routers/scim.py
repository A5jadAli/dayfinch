from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from ..web import normalize_email

router = APIRouter(prefix="/scim/v2", tags=["scim"])

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


class SCIMError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        detail: str,
        scim_type: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type
        self.headers = headers or {}
        super().__init__(detail)


def scim_error_response(error: SCIMError) -> JSONResponse:
    payload = {
        "schemas": [ERROR_SCHEMA],
        "status": str(error.status_code),
        "detail": error.detail,
    }
    if error.scim_type:
        payload["scimType"] = error.scim_type
    return JSONResponse(
        payload,
        status_code=error.status_code,
        headers=error.headers,
        media_type="application/scim+json",
    )


def _authorize(request: Request, authorization: str) -> None:
    expected = request.app.state.settings.scim_bearer_token
    scheme, _, supplied = authorization.partition(" ")
    if (
        not expected
        or scheme.lower() != "bearer"
        or not supplied
        or not hmac.compare_digest(supplied, expected)
    ):
        raise SCIMError(
            401,
            "A valid SCIM bearer token is required",
            headers={"WWW-Authenticate": 'Bearer realm="Dayfinch SCIM"'},
        )


def _domain_email(request: Request, value: Any) -> str:
    try:
        email = normalize_email(str(value))
    except ValueError as exc:
        raise SCIMError(
            400, "userName must be a valid email address", "invalidValue"
        ) from exc
    required = str(request.app.state.database.organization_settings()["sso_domain"])
    required = required.strip().lower().lstrip("@")
    if not required:
        raise SCIMError(
            503, "Configure the workspace SSO domain before provisioning users"
        )
    if email.rsplit("@", 1)[1] != required:
        raise SCIMError(
            400, "userName is outside the workspace SSO domain", "invalidValue"
        )
    return email


def _display_name(data: dict[str, Any]) -> str:
    name = data.get("name")
    formatted = name.get("formatted") if isinstance(name, dict) else ""
    return str(data.get("displayName") or formatted or "")[:120]


def _active(data: dict[str, Any], default: bool = True) -> bool:
    value = data.get("active", default)
    if not isinstance(value, bool):
        raise SCIMError(400, "active must be a boolean", "invalidValue")
    return value


def _version(user: dict[str, Any]) -> str:
    value = str(user.get("scim_updated_at") or user.get("created_at") or user["id"])
    return f'W/"{hashlib.sha256(value.encode()).hexdigest()[:24]}"'


def _user_resource(request: Request, user: dict[str, Any]) -> dict[str, Any]:
    location = f"{request.app.state.settings.public_url}/scim/v2/Users/{user['id']}"
    return {
        "schemas": [USER_SCHEMA],
        "id": user["id"],
        "externalId": user.get("scim_external_id") or "",
        "userName": user["email"],
        "displayName": user.get("full_name") or user["email"],
        "name": {"formatted": user.get("full_name") or ""},
        "emails": [{"value": user["email"], "type": "work", "primary": True}],
        "active": bool(user["enabled"]),
        "meta": {
            "resourceType": "User",
            "created": user["created_at"],
            "lastModified": user.get("scim_updated_at") or user["created_at"],
            "version": _version(user),
            "location": location,
        },
    }


def _group_version(group: dict[str, Any]) -> str:
    value = str(group.get("scim_updated_at") or group.get("created_at") or group["id"])
    return f'W/"{hashlib.sha256(value.encode()).hexdigest()[:24]}"'


def _group_resource(request: Request, group: dict[str, Any]) -> dict[str, Any]:
    location = f"{request.app.state.settings.public_url}/scim/v2/Groups/{group['id']}"
    members = [
        {
            "value": member["value"],
            "display": member.get("display", ""),
            "$ref": f"{request.app.state.settings.public_url}/scim/v2/Users/{member['value']}",
        }
        for member in group.get("members", [])
    ]
    return {
        "schemas": [GROUP_SCHEMA],
        "id": group["id"],
        "externalId": group.get("scim_external_id") or "",
        "displayName": group["name"],
        "members": members,
        "meta": {
            "resourceType": "Group",
            "created": group["created_at"],
            "lastModified": group["scim_updated_at"],
            "version": _group_version(group),
            "location": location,
        },
    }


def _member_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SCIMError(400, "members must be an array", "invalidValue")
    result = []
    for member in value:
        if not isinstance(member, dict) or not str(member.get("value", "")).strip():
            raise SCIMError(400, "Every member requires a value", "invalidValue")
        result.append(str(member["value"]).strip())
    return result


def _raise_group_value_error(error: ValueError) -> None:
    if "member" in str(error).lower():
        raise SCIMError(400, str(error), "invalidValue") from error
    raise SCIMError(409, str(error), "uniqueness") from error


def _response(
    payload: Any, status_code: int = 200, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers=headers,
        media_type="application/scim+json",
    )


async def _payload(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except ValueError as exc:
        raise SCIMError(
            400, "The request body must be valid JSON", "invalidSyntax"
        ) from exc
    if not isinstance(value, dict):
        raise SCIMError(400, "The request body must be a JSON object", "invalidSyntax")
    return value


@router.get("/ServiceProviderConfig")
def service_provider_config(request: Request, authorization: str = Header(default="")):
    _authorize(request, authorization)
    return _response(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "Deployment-managed SCIM bearer token",
                    "primary": True,
                }
            ],
        }
    )


@router.get("/ResourceTypes")
def resource_types(request: Request, authorization: str = Header(default="")):
    _authorize(request, authorization)
    return _response(
        [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "schema": USER_SCHEMA,
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "Group",
                "name": "Group",
                "endpoint": "/Groups",
                "schema": GROUP_SCHEMA,
            },
        ]
    )


@router.get("/Schemas")
def schemas(request: Request, authorization: str = Header(default="")):
    _authorize(request, authorization)
    return _response(
        [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
                "id": USER_SCHEMA,
                "name": "User",
                "description": "Dayfinch SCIM user",
                "attributes": [],
            },
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
                "id": GROUP_SCHEMA,
                "name": "Group",
                "description": "Dayfinch SCIM team",
                "attributes": [],
            },
        ]
    )


@router.get("/Users")
def list_users(
    request: Request,
    authorization: str = Header(default=""),
    filter: str = Query(default=""),
    start_index: int = Query(default=1, alias="startIndex", ge=1),
    count: int = Query(default=100, ge=0, le=200),
):
    _authorize(request, authorization)
    username = None
    if filter:
        match = re.fullmatch(r'\s*userName\s+eq\s+"([^"]+)"\s*', filter, re.IGNORECASE)
        if not match:
            raise SCIMError(
                400, "Only the userName eq filter is supported", "invalidFilter"
            )
        username = _domain_email(request, match.group(1))
    users, total = request.app.state.database.list_scim_users(
        username, start_index, count
    )
    resources = [_user_resource(request, user) for user in users]
    return _response(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": resources,
        }
    )


@router.get("/Users/{user_id}")
def get_user(request: Request, user_id: str, authorization: str = Header(default="")):
    _authorize(request, authorization)
    user = request.app.state.database.get_scim_user(user_id)
    if not user:
        raise SCIMError(404, "SCIM user not found")
    resource = _user_resource(request, user)
    return _response(resource, headers={"ETag": resource["meta"]["version"]})


@router.post("/Users")
async def create_user(request: Request, authorization: str = Header(default="")):
    _authorize(request, authorization)
    data = await _payload(request)
    if USER_SCHEMA not in data.get("schemas", []):
        raise SCIMError(400, "The SCIM User schema is required", "invalidSyntax")
    email = _domain_email(request, data.get("userName", ""))
    try:
        user = request.app.state.database.create_scim_user(
            email,
            str(data.get("externalId", ""))[:512],
            _display_name(data),
            _active(data),
        )
    except ValueError as exc:
        raise SCIMError(409, str(exc), "uniqueness") from exc
    request.app.state.database.add_audit_event(
        None, "scim.user_created", "user", user["id"], user["email"]
    )
    resource = _user_resource(request, user)
    return _response(
        resource,
        status_code=201,
        headers={
            "Location": resource["meta"]["location"],
            "ETag": resource["meta"]["version"],
        },
    )


@router.put("/Users/{user_id}")
async def replace_user(
    request: Request, user_id: str, authorization: str = Header(default="")
):
    _authorize(request, authorization)
    data = await _payload(request)
    if USER_SCHEMA not in data.get("schemas", []):
        raise SCIMError(400, "The SCIM User schema is required", "invalidSyntax")
    email = _domain_email(request, data.get("userName", ""))
    try:
        user = request.app.state.database.update_scim_user(
            user_id,
            email=email,
            external_id=str(data.get("externalId", ""))[:512],
            display_name=_display_name(data),
            enabled=_active(data),
        )
    except LookupError as exc:
        raise SCIMError(404, str(exc)) from exc
    except ValueError as exc:
        raise SCIMError(409, str(exc), "uniqueness") from exc
    request.app.state.database.add_audit_event(
        None, "scim.user_replaced", "user", user_id, user["email"]
    )
    resource = _user_resource(request, user)
    return _response(resource, headers={"ETag": resource["meta"]["version"]})


@router.patch("/Users/{user_id}")
async def patch_user(
    request: Request, user_id: str, authorization: str = Header(default="")
):
    _authorize(request, authorization)
    data = await _payload(request)
    if PATCH_SCHEMA not in data.get("schemas", []):
        raise SCIMError(400, "The SCIM PatchOp schema is required", "invalidSyntax")
    changes: dict[str, Any] = {}
    operations = data.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise SCIMError(400, "Operations must be a non-empty array", "invalidSyntax")
    for operation in operations:
        if (
            not isinstance(operation, dict)
            or str(operation.get("op", "")).lower() != "replace"
        ):
            raise SCIMError(
                400, "Only replace PATCH operations are supported", "invalidSyntax"
            )
        path = str(operation.get("path", "")).lower()
        value = operation.get("value")
        if not path and isinstance(value, dict):
            for key, nested_value in value.items():
                normalized_key = str(key).lower()
                if normalized_key == "active" and isinstance(nested_value, bool):
                    changes["enabled"] = nested_value
                elif normalized_key == "username":
                    changes["email"] = _domain_email(request, nested_value)
                elif normalized_key == "displayname":
                    changes["display_name"] = str(nested_value)[:120]
                elif normalized_key == "externalid":
                    changes["external_id"] = str(nested_value)[:512]
                else:
                    raise SCIMError(
                        400,
                        f"Unsupported PATCH path: {normalized_key}",
                        "invalidPath",
                    )
            continue
        if path == "active" and isinstance(value, bool):
            changes["enabled"] = value
        elif path == "username":
            changes["email"] = _domain_email(request, value)
        elif path == "displayname":
            changes["display_name"] = str(value)[:120]
        elif path == "externalid":
            changes["external_id"] = str(value)[:512]
        else:
            raise SCIMError(
                400, f"Unsupported PATCH path: {path or '(missing)'}", "invalidPath"
            )
    try:
        user = request.app.state.database.update_scim_user(user_id, **changes)
    except LookupError as exc:
        raise SCIMError(404, str(exc)) from exc
    except ValueError as exc:
        raise SCIMError(409, str(exc), "uniqueness") from exc
    request.app.state.database.add_audit_event(
        None, "scim.user_patched", "user", user_id, ",".join(sorted(changes))
    )
    resource = _user_resource(request, user)
    return _response(resource, headers={"ETag": resource["meta"]["version"]})


@router.delete("/Users/{user_id}", status_code=204)
def delete_user(
    request: Request, user_id: str, authorization: str = Header(default="")
):
    _authorize(request, authorization)
    try:
        request.app.state.database.update_scim_user(user_id, enabled=False)
    except LookupError as exc:
        raise SCIMError(404, str(exc)) from exc
    request.app.state.database.add_audit_event(
        None, "scim.user_deactivated", "user", user_id
    )
    return Response(status_code=204)


@router.get("/Groups")
def list_groups(
    request: Request,
    authorization: str = Header(default=""),
    filter: str = Query(default=""),
    start_index: int = Query(default=1, alias="startIndex", ge=1),
    count: int = Query(default=100, ge=0, le=200),
):
    _authorize(request, authorization)
    display_name = None
    if filter:
        match = re.fullmatch(
            r'\s*displayName\s+eq\s+"([^"]+)"\s*', filter, re.IGNORECASE
        )
        if not match:
            raise SCIMError(
                400,
                "Only the displayName eq group filter is supported",
                "invalidFilter",
            )
        display_name = match.group(1)
    groups, total = request.app.state.database.list_scim_groups(
        display_name, start_index, count
    )
    resources = [_group_resource(request, group) for group in groups]
    return _response(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": resources,
        }
    )


@router.get("/Groups/{group_id}")
def get_group(request: Request, group_id: str, authorization: str = Header(default="")):
    _authorize(request, authorization)
    group = request.app.state.database.get_scim_group(group_id)
    if not group:
        raise SCIMError(404, "SCIM group not found")
    resource = _group_resource(request, group)
    return _response(resource, headers={"ETag": resource["meta"]["version"]})


@router.post("/Groups")
async def create_group(request: Request, authorization: str = Header(default="")):
    _authorize(request, authorization)
    data = await _payload(request)
    if GROUP_SCHEMA not in data.get("schemas", []):
        raise SCIMError(400, "The SCIM Group schema is required", "invalidSyntax")
    name = str(data.get("displayName", "")).strip()
    if not name:
        raise SCIMError(400, "displayName is required", "invalidValue")
    try:
        group = request.app.state.database.create_scim_group(
            name, str(data.get("externalId", "")), _member_ids(data.get("members"))
        )
    except ValueError as exc:
        _raise_group_value_error(exc)
    request.app.state.database.add_audit_event(
        None, "scim.group_created", "team", group["id"], group["name"]
    )
    resource = _group_resource(request, group)
    return _response(
        resource,
        status_code=201,
        headers={
            "Location": resource["meta"]["location"],
            "ETag": resource["meta"]["version"],
        },
    )


@router.put("/Groups/{group_id}")
async def replace_group(
    request: Request, group_id: str, authorization: str = Header(default="")
):
    _authorize(request, authorization)
    data = await _payload(request)
    if GROUP_SCHEMA not in data.get("schemas", []):
        raise SCIMError(400, "The SCIM Group schema is required", "invalidSyntax")
    name = str(data.get("displayName", "")).strip()
    if not name:
        raise SCIMError(400, "displayName is required", "invalidValue")
    try:
        group = request.app.state.database.update_scim_group(
            group_id,
            name=name,
            external_id=str(data.get("externalId", "")),
            member_ids=_member_ids(data.get("members")),
        )
    except LookupError as exc:
        raise SCIMError(404, str(exc)) from exc
    except ValueError as exc:
        _raise_group_value_error(exc)
    request.app.state.database.add_audit_event(
        None, "scim.group_replaced", "team", group_id, group["name"]
    )
    resource = _group_resource(request, group)
    return _response(resource, headers={"ETag": resource["meta"]["version"]})


@router.patch("/Groups/{group_id}")
async def patch_group(
    request: Request, group_id: str, authorization: str = Header(default="")
):
    _authorize(request, authorization)
    data = await _payload(request)
    if PATCH_SCHEMA not in data.get("schemas", []):
        raise SCIMError(400, "The SCIM PatchOp schema is required", "invalidSyntax")
    group = request.app.state.database.get_scim_group(group_id)
    if not group:
        raise SCIMError(404, "SCIM group not found")
    member_ids = [member["value"] for member in group.get("members", [])]
    name = None
    external_id = None
    operations = data.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise SCIMError(400, "Operations must be a non-empty array", "invalidSyntax")
    for operation in operations:
        if not isinstance(operation, dict):
            raise SCIMError(400, "PATCH operation is invalid", "invalidSyntax")
        action = str(operation.get("op", "")).lower()
        path = str(operation.get("path", ""))
        value = operation.get("value")
        if action == "replace" and path.lower() == "displayname":
            name = str(value).strip()
            if not name:
                raise SCIMError(400, "displayName is required", "invalidValue")
        elif action == "replace" and path.lower() == "externalid":
            external_id = str(value)
        elif path.lower() == "members" and action in {"add", "replace", "remove"}:
            values = _member_ids(value)
            if action == "replace":
                member_ids = values
            elif action == "add":
                member_ids = list(dict.fromkeys([*member_ids, *values]))
            else:
                member_ids = [item for item in member_ids if item not in set(values)]
        elif action == "remove":
            match = re.fullmatch(
                r'members\[value\s+eq\s+"([^"]+)"\]', path, re.IGNORECASE
            )
            if not match:
                raise SCIMError(400, f"Unsupported PATCH path: {path}", "invalidPath")
            member_ids = [item for item in member_ids if item != match.group(1)]
        else:
            raise SCIMError(400, f"Unsupported PATCH path: {path}", "invalidPath")
    try:
        group = request.app.state.database.update_scim_group(
            group_id,
            name=name,
            external_id=external_id,
            member_ids=member_ids,
        )
    except LookupError as exc:
        raise SCIMError(404, str(exc)) from exc
    except ValueError as exc:
        raise SCIMError(400, str(exc), "invalidValue") from exc
    request.app.state.database.add_audit_event(
        None, "scim.group_patched", "team", group_id
    )
    resource = _group_resource(request, group)
    return _response(resource, headers={"ETag": resource["meta"]["version"]})


@router.delete("/Groups/{group_id}", status_code=204)
def delete_group(
    request: Request, group_id: str, authorization: str = Header(default="")
):
    _authorize(request, authorization)
    try:
        request.app.state.database.delete_scim_group(group_id)
    except LookupError as exc:
        raise SCIMError(404, str(exc)) from exc
    request.app.state.database.add_audit_event(
        None, "scim.group_deleted", "team", group_id
    )
    return Response(status_code=204)
