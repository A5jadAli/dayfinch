from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ..schemas import AutomaticTrackingConsent, Heartbeat, LocationPoint, UsagePoint
from ..services.privacy import normalize_domain
from .dependencies import device_from_authorization

router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.post("/usage", status_code=201)
def ingest_usage(
    request: Request,
    payload: UsagePoint,
    device: dict[str, Any] = Depends(device_from_authorization),
) -> dict[str, str]:
    if device.get("tracker_kind", "desktop") != "desktop":
        raise HTTPException(
            status_code=403, detail="Activity collection requires a desktop tracker"
        )
    observed = payload.observed_at
    if observed.tzinfo is None:
        raise HTTPException(status_code=422, detail="observed_at must include timezone")
    observed = observed.astimezone(UTC)
    now = datetime.now(UTC)
    if observed > now + timedelta(minutes=5) or observed < now - timedelta(days=90):
        raise HTTPException(
            status_code=422, detail="usage timestamp is outside replay window"
        )
    database = request.app.state.database
    session = database.get_work_session_for_capture(device["id"], observed)
    if not session:
        raise HTTPException(
            status_code=409,
            detail="No tracked work session covers this usage sample",
        )
    policy = database.effective_tracking_settings(session["user_id"])
    created = database.add_usage_record(
        {
            "id": str(payload.event_id),
            "device_id": device["id"],
            "user_id": session["user_id"] if session else device.get("owner_user_id"),
            "project_id": session["project_id"]
            if session
            else device.get("project_id"),
            "task_id": session["task_id"] if session else None,
            "session_id": session["id"] if session else None,
            "observed_at": observed,
            "active_app": payload.active_app.strip() or None
            if policy["track_apps"]
            else None,
            "active_url": normalize_domain(payload.active_url) or None
            if policy["track_urls"]
            else None,
            "focused_seconds": payload.focused_seconds,
        }
    )
    return {"status": "created" if created else "duplicate"}


@router.post("/location", status_code=201)
def ingest_location(
    request: Request,
    payload: LocationPoint,
    device: dict[str, Any] = Depends(device_from_authorization),
) -> dict[str, str]:
    recorded = payload.recorded_at
    if recorded.tzinfo is None:
        raise HTTPException(status_code=422, detail="recorded_at must include timezone")
    recorded = recorded.astimezone(UTC)
    now = datetime.now(UTC)
    if recorded > now + timedelta(minutes=5) or recorded < now - timedelta(days=90):
        raise HTTPException(
            status_code=422, detail="location timestamp is outside replay window"
        )
    session = request.app.state.database.get_work_session_for_capture(
        device["id"], recorded
    )
    if not session:
        raise HTTPException(
            status_code=409,
            detail="No tracked work session covers this location sample",
        )
    created = request.app.state.database.add_location(
        {
            "id": str(payload.event_id),
            "device_id": device["id"],
            "user_id": device.get("owner_user_id"),
            "session_id": session["id"],
            "recorded_at": recorded,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "accuracy_meters": payload.accuracy_meters,
            "event_type": payload.event_type,
        }
    )
    return {"status": "created" if created else "duplicate"}


@router.get("/configuration")
def configuration(
    request: Request,
    device: dict[str, Any] = Depends(device_from_authorization),
) -> dict[str, Any]:
    """Return centrally managed collection policy and the member's work catalog."""
    database = request.app.state.database
    owner = database.get_user(device.get("owner_user_id"))
    settings = database.effective_tracking_settings(owner["id"] if owner else None)
    projects = (
        database.list_trackable_projects(
            owner["id"] if owner["role"] == "member" else None
        )
        if owner
        else []
    )
    automatic = database.automatic_tracking_for_user(owner["id"]) if owner else None
    return {
        "screenshot_frequency": settings["screenshot_frequency"],
        "screenshot_blur": settings["screenshot_blur"],
        "track_apps": settings["track_apps"],
        "track_urls": settings["track_urls"],
        "allowed_apps": settings["allowed_apps"],
        "idle_timeout_minutes": settings["idle_timeout_minutes"],
        "projects": [
            {
                "id": project["id"],
                "name": project["name"],
                "tasks": database.list_tasks(
                    project["id"], user_id=owner["id"] if owner else None
                ),
            }
            for project in projects
        ],
        "automatic_tracking": (
            {
                "id": automatic["id"],
                "name": automatic["name"],
                "rule_type": automatic["rule_type"],
                "project_id": automatic["project_id"],
                "wait_for_activity": automatic["wait_for_activity"],
                "schedule": automatic["schedule"],
                "consent_status": automatic["consent_status"],
                "updated_at": automatic["updated_at"],
                "shift_windows": automatic["shift_windows"],
            }
            if automatic
            else None
        ),
    }


@router.post("/automatic-tracking/consent")
def automatic_tracking_consent(
    request: Request,
    payload: AutomaticTrackingConsent,
    device: dict[str, Any] = Depends(device_from_authorization),
) -> dict[str, str]:
    owner_id = device.get("owner_user_id")
    if (
        not owner_id
        or not request.app.state.database.respond_to_automatic_tracking_policy(
            owner_id, str(payload.policy_id), payload.accepted
        )
    ):
        raise HTTPException(
            status_code=404, detail="Automatic tracking policy not found"
        )
    request.app.state.database.add_audit_event(
        owner_id,
        "automatic_tracking.accepted"
        if payload.accepted
        else "automatic_tracking.declined",
        "automatic_tracking_policy",
        str(payload.policy_id),
    )
    return {"status": "accepted" if payload.accepted else "declined"}


@router.post("/heartbeat")
def heartbeat(
    request: Request,
    payload: Heartbeat,
    device: dict[str, Any] = Depends(device_from_authorization),
) -> dict[str, str | None]:
    database = request.app.state.database
    server_now = datetime.now(UTC)
    observed = payload.observed_at or server_now
    if observed.tzinfo is None:
        raise HTTPException(
            status_code=422, detail="observed_at must include a timezone"
        )
    observed = observed.astimezone(UTC)
    if observed > server_now + timedelta(minutes=5):
        raise HTTPException(
            status_code=422, detail="observed_at is too far in the future"
        )
    if observed < server_now - timedelta(days=90):
        raise HTTPException(status_code=422, detail="observed_at is too old to replay")
    task_id = str(payload.task_id) if payload.task_id else None
    try:
        session = database.sync_work_session(
            device,
            payload.status,
            task_id,
            str(payload.project_id) if payload.project_id else None,
            payload.note,
            event_id=str(payload.event_id) if payload.event_id else None,
            observed_at=observed,
            idle_seconds=payload.idle_seconds,
            heartbeat_interval_seconds=payload.heartbeat_interval_seconds,
            transition=payload.transition,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.touch_device(device["id"], payload.platform, payload.status)
    return {"status": "ok", "session_id": session["id"] if session else None}


@router.post("/activity", status_code=201)
async def ingest_activity(
    request: Request,
    record_id: Annotated[str, Form()],
    captured_at: Annotated[str, Form()],
    keyboard_events: Annotated[int, Form(ge=0, le=1_000_000)],
    mouse_clicks: Annotated[int, Form(ge=0, le=1_000_000)],
    mouse_distance: Annotated[int, Form(ge=0, le=1_000_000_000)],
    active_app: Annotated[str, Form(max_length=160)],
    agent_version: Annotated[str, Form(min_length=1, max_length=40)],
    screenshot_file: Annotated[UploadFile, File()],
    device: dict[str, Any] = Depends(device_from_authorization),
    focused_seconds: Annotated[int, Form(ge=0, le=86_400)] = 0,
    interactive_seconds: Annotated[int, Form(ge=0, le=86_400)] = 0,
    session_id: Annotated[str, Form(max_length=40)] = "",
    active_url: Annotated[str, Form(max_length=255)] = "",
    automation_suspected: Annotated[bool, Form()] = False,
    screenshot_blurred: Annotated[bool, Form()] = False,
) -> dict[str, str]:
    if device.get("tracker_kind", "desktop") != "desktop":
        raise HTTPException(
            status_code=403, detail="Activity collection requires a desktop tracker"
        )
    database = request.app.state.database
    storage = request.app.state.storage
    settings = request.app.state.settings
    policy = database.effective_tracking_settings(device.get("owner_user_id"))
    if policy["screenshot_frequency"] <= 0:
        raise HTTPException(
            status_code=403, detail="Screenshot collection is disabled by policy"
        )
    if policy["screenshot_blur"] and not screenshot_blurred:
        raise HTTPException(
            status_code=403, detail="Screenshot blur is required by policy"
        )
    try:
        parsed_id = str(uuid.UUID(record_id))
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise ValueError("timezone required")
        captured = captured.astimezone(UTC)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid record id or timestamp"
        ) from exc

    if database.record_exists(parsed_id):
        return {"status": "duplicate", "record_id": parsed_id}

    now = datetime.now(UTC)
    if captured > now + timedelta(minutes=5) or captured < now - timedelta(days=90):
        raise HTTPException(
            status_code=422, detail="capture timestamp is outside replay window"
        )

    session = database.get_work_session(session_id) if session_id else None
    if session and session["device_id"] != device["id"]:
        raise HTTPException(status_code=422, detail="Session does not belong to device")
    # captured_at, not an old in-memory session id, is authoritative after an
    # offline task switch or process restart.
    session = database.get_work_session_for_capture(device["id"], captured)
    if not session:
        raise HTTPException(
            status_code=409,
            detail="No tracked work session covers this screenshot",
        )

    data = await screenshot_file.read(settings.max_upload_bytes + 1)
    if not data or len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Screenshot is empty or too large")
    try:
        # A random object suffix prevents concurrent retries for the same record
        # from overwriting and then deleting the already-accepted screenshot.
        storage_record_id = f"{parsed_id}-{uuid.uuid4()}"
        stored = await asyncio.to_thread(
            storage.save, device["id"], storage_record_id, captured, data
        )
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    created = await asyncio.to_thread(
        database.add_record,
        {
            "id": parsed_id,
            "device_id": device["id"],
            "captured_at": captured.isoformat(),
            "keyboard_events": keyboard_events,
            "mouse_clicks": mouse_clicks,
            "mouse_distance": mouse_distance,
            "active_app": active_app.strip()[:160] or None
            if policy["track_apps"]
            else None,
            "agent_version": agent_version,
            "screenshot_path": stored.key,
            "storage_version_id": stored.version_id,
            "focused_seconds": focused_seconds,
            # Suspected synthetic input never counts as genuine interaction, whatever
            # the agent reported, so faking activity cannot inflate the number.
            "interactive_seconds": 0
            if automation_suspected
            else min(interactive_seconds, focused_seconds),
            "active_url": (normalize_domain(active_url) or None)
            if policy["track_urls"]
            else None,
            "automation_suspected": automation_suspected,
            "activity_percent": 0
            if automation_suspected or focused_seconds <= 0
            else min(100, round(interactive_seconds * 100 / focused_seconds)),
            "source": "desktop",
            "screenshot_blurred": screenshot_blurred,
            "user_id": session["user_id"] if session else device.get("owner_user_id"),
            "project_id": session["project_id"]
            if session
            else device.get("project_id"),
            "task_id": session["task_id"] if session else None,
            "session_id": session["id"] if session else None,
        },
    )
    if not created:
        await asyncio.to_thread(storage.delete, stored.key, stored.version_id)
    await asyncio.to_thread(database.touch_device, device["id"], "", "active")
    return {"status": "created" if created else "duplicate", "record_id": parsed_id}
