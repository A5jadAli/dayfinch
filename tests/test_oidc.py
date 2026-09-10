from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.services.oidc import OIDCAuthenticationError, OIDCService


def _settings(tmp_path, postgres_url) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="admin@example.test",
        database_url=postgres_url,
        oidc_issuer="https://identity.example.test",
        oidc_client_id="dayfinch",
        oidc_client_secret="provider-secret",
        oidc_end_session_url="https://identity.example.test/logout",
    )


def _enable_sso(database, domain: str = "example.test") -> None:
    values = database.organization_settings()
    values.update(sso_provider="OpenID Connect", sso_domain=domain)
    database.update_organization_settings(values)


class FakeOIDC:
    enabled = True

    def __init__(self, identity):
        self.identity = identity

    async def begin(self, _request):
        return RedirectResponse("https://identity.example.test/authorize", 302)

    async def authenticate(self, _request):
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def logout_url(self):
        return "https://identity.example.test/logout?post_logout_redirect_uri=safe"


def test_oidc_links_only_an_existing_invited_account(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        _enable_sso(database)
        app.state.oidc = FakeOIDC(
            {
                "issuer": settings.oidc_issuer,
                "subject": "admin-subject",
                "email": settings.admin_email,
            }
        )

        login = client.get("/login")
        start = client.get("/auth/oidc", follow_redirects=False)
        callback = client.get("/auth/oidc/callback", follow_redirects=False)

        assert "Continue with company SSO" in login.text
        assert start.headers["location"].startswith("https://identity.example.test")
        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        linked = database.get_user_by_email(settings.admin_email)
        assert linked["sso_issuer"] == settings.oidc_issuer
        assert linked["sso_subject"] == "admin-subject"
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        csrf = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
        logout = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
        assert logout.status_code == 303
        assert logout.headers["location"].startswith(
            "https://identity.example.test/logout"
        )


def test_oidc_rejects_unknown_and_out_of_domain_identities(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        _enable_sso(database)
        app.state.oidc = FakeOIDC(
            {
                "issuer": settings.oidc_issuer,
                "subject": "unknown",
                "email": "unknown@example.test",
            }
        )
        unknown = client.get("/auth/oidc/callback")
        assert unknown.status_code == 401
        assert "request an invitation" in unknown.text

        app.state.oidc.identity = {
            "issuer": settings.oidc_issuer,
            "subject": "admin-subject",
            "email": settings.admin_email,
        }
        _enable_sso(database, "different.test")
        wrong_domain = client.get("/auth/oidc/callback")
        assert wrong_domain.status_code == 401
        assert "outside this workspace" in wrong_domain.text


def test_oidc_profile_requires_matching_issuer_and_verified_email(
    tmp_path, postgres_url, monkeypatch
):
    settings = _settings(tmp_path, postgres_url)
    service = OIDCService(settings)

    class Client:
        async def authorize_access_token(self, _request):
            return {
                "userinfo": {
                    "iss": "https://attacker.example.test",
                    "sub": "subject",
                    "email": "admin@example.test",
                    "email_verified": True,
                }
            }

    monkeypatch.setattr(service, "_client", lambda: Client())
    with pytest.raises(OIDCAuthenticationError, match="issuer"):
        asyncio.run(service.authenticate(None))


def test_oidc_logout_url_is_fixed_to_the_public_login(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    logout = urlparse(OIDCService(settings).logout_url())
    assert (
        f"{logout.scheme}://{logout.netloc}{logout.path}"
        == settings.oidc_end_session_url
    )
    parameters = parse_qs(logout.query)
    assert parameters == {
        "client_id": [settings.oidc_client_id],
        "post_logout_redirect_uri": [f"{settings.public_url}/login"],
    }


def test_oidc_configuration_must_be_complete_and_https_in_production(
    tmp_path, postgres_url
):
    incomplete = replace(
        _settings(tmp_path, postgres_url),
        oidc_client_secret="",
    )
    with pytest.raises(RuntimeError, match="configured together"):
        incomplete.prepare()

    insecure = replace(
        _settings(tmp_path, postgres_url),
        environment="production",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
        cookie_secure=True,
        oidc_issuer="http://identity.example.test",
    )
    insecure.prepare()
    with pytest.raises(RuntimeError, match="OIDC issuer"):
        insecure.validate_for_nonlocal()

    unsafe_logout = replace(
        _settings(tmp_path, postgres_url),
        oidc_end_session_url="https://identity.example.test/logout?next=attacker",
    )
    with pytest.raises(RuntimeError, match="END_SESSION_URL"):
        unsafe_logout.prepare()


def test_sso_identity_cannot_be_reassigned(database):
    first = database.bootstrap_admin("first@example.test", "hash")
    _, invitation = database.create_invitation("second@example.test", first["id"], 24)
    second = database.accept_invitation(invitation, "hash")
    database.link_sso_identity(first["id"], "https://issuer.test", "same-subject")

    with pytest.raises(ValueError, match="another user"):
        database.link_sso_identity(second["id"], "https://issuer.test", "same-subject")
