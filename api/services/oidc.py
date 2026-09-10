from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import Response

from ..config import Settings


class OIDCAuthenticationError(RuntimeError):
    pass


class OIDCService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.oidc_enabled
        self._oauth = OAuth()
        if self.enabled:
            self._oauth.register(
                name="dayfinch_oidc",
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                server_metadata_url=settings.oidc_metadata_url,
                client_kwargs={
                    "scope": "openid email profile",
                    "code_challenge_method": "S256",
                    "token_endpoint_auth_method": settings.oidc_client_auth_method,
                },
            )

    def _client(self):
        if not self.enabled:
            raise OIDCAuthenticationError("Single sign-on is not configured")
        return self._oauth.create_client("dayfinch_oidc")

    async def begin(self, request: Request) -> Response:
        redirect_uri = f"{self.settings.public_url}/auth/oidc/callback"
        try:
            return await self._client().authorize_redirect(request, redirect_uri)
        except (OAuthError, httpx.HTTPError, ValueError) as exc:
            raise OIDCAuthenticationError(
                "Single sign-on is temporarily unavailable"
            ) from exc

    async def authenticate(self, request: Request) -> dict[str, Any]:
        try:
            token = await self._client().authorize_access_token(request)
        except (OAuthError, httpx.HTTPError, ValueError) as exc:
            raise OIDCAuthenticationError(
                "The identity provider rejected the sign-in"
            ) from exc
        profile = token.get("userinfo")
        if not isinstance(profile, Mapping):
            raise OIDCAuthenticationError("The identity provider returned no identity")
        issuer = str(profile.get("iss", "")).rstrip("/")
        subject = str(profile.get("sub", "")).strip()
        email = str(profile.get("email", "")).strip().lower()
        if not hmac.compare_digest(issuer, self.settings.oidc_issuer):
            raise OIDCAuthenticationError("The identity provider issuer did not match")
        if not subject:
            raise OIDCAuthenticationError("The identity provider returned no subject")
        if profile.get("email_verified") is not True:
            raise OIDCAuthenticationError(
                "The identity provider did not verify the email"
            )
        if email.count("@") != 1:
            raise OIDCAuthenticationError(
                "The identity provider returned an invalid email"
            )
        return {"issuer": issuer, "subject": subject, "email": email}

    def logout_url(self) -> str:
        endpoint = self.settings.oidc_end_session_url
        if not endpoint:
            return ""
        return f"{endpoint}?{urlencode({'client_id': self.settings.oidc_client_id, 'post_logout_redirect_uri': f'{self.settings.public_url}/login'})}"
