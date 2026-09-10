from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings
from .database import Database
from .middleware import RequestRateLimitMiddleware, SecurityHeadersMiddleware
from .observability import MetricsRegistry, ObservabilityMiddleware, configure_logging
from .routers.agent_api import router as agent_api_router
from .routers.auth import router as auth_router
from .routers.dashboard import router as dashboard_router
from .routers.devices import router as devices_router
from .routers.integrations import router as integrations_router
from .routers.projects import router as projects_router
from .routers.reports import router as reports_router
from .routers.scim import SCIMError, scim_error_response
from .routers.scim import router as scim_router
from .routers.timesheets import router as timesheets_router
from .routers.workforce import router as workforce_router
from .security import hash_password
from .services.asana_integration import AsanaCloudService
from .services.background_jobs import BackgroundJobCoordinator, run_periodic_job
from .services.github_integration import GitHubAppService
from .services.integration_credentials import (
    CredentialKeyring,
    IntegrationCredentialVault,
)
from .services.invitation_delivery import InvitationDeliveryService
from .services.invoice_vault import InvoiceVault
from .services.jira_integration import JiraCloudService
from .services.oidc import OIDCService
from .services.payments import PayrollDeliveryService
from .services.report_delivery import ReportDeliveryService
from .services.retention import RetentionService
from .services.saml import SAMLService
from .services.slack_integration import SlackCloudService
from .services.timesheets import TimesheetService
from .storage import create_storage
from .web import WebSecurity

PACKAGE_DIR = Path(__file__).resolve().parent
UI_DIR = PACKAGE_DIR.parent / "ui"
LOGGER = logging.getLogger("dayfinch-server")


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in (
        "app.css",
        "app.js",
        "dayfinch-icon.svg",
        "field.js",
        "manifest.webmanifest",
        "service-worker.js",
    ):
        digest.update(name.encode())
        digest.update((UI_DIR / "static" / name).read_bytes())
    return digest.hexdigest()[:12]


def create_app(
    settings: Settings | None = None,
    *,
    github_transport=None,
    jira_transport=None,
    asana_transport=None,
    slack_transport=None,
    payment_transport=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    configure_logging(settings.log_level)
    if settings.environment == "production":
        settings.validate_for_nonlocal()
    database = Database(
        settings.database_url,
        min_pool_size=settings.database_min_pool_size,
        max_pool_size=settings.database_max_pool_size,
    )
    storage = create_storage(settings)
    retention = RetentionService(
        database,
        storage,
        settings.retention_days,
        settings.audit_retention_days,
    )
    report_delivery = ReportDeliveryService(database, settings)
    payroll_delivery = PayrollDeliveryService(
        database, settings, transport=payment_transport
    )
    invoice_vault = InvoiceVault(database, storage, settings)
    invitation_delivery = InvitationDeliveryService(settings)
    oidc = OIDCService(settings)
    saml = SAMLService(settings)
    github = GitHubAppService(settings, database, transport=github_transport)
    integration_credentials = (
        IntegrationCredentialVault(
            database, CredentialKeyring.parse(settings.integration_encryption_keys)
        )
        if settings.integration_encryption_keys
        else None
    )
    jira = JiraCloudService(
        settings,
        database,
        integration_credentials,
        transport=jira_transport,
    )
    asana = AsanaCloudService(
        settings,
        database,
        integration_credentials,
        transport=asana_transport,
    )
    slack = SlackCloudService(
        settings,
        database,
        integration_credentials,
        transport=slack_transport,
    )
    metrics = MetricsRegistry()
    job_coordinator = BackgroundJobCoordinator(database, str(uuid4()), metrics)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        database.bootstrap_admin(
            settings.admin_email, hash_password(settings.admin_password)
        )
        retention_task = asyncio.create_task(
            run_periodic_job(
                job_coordinator,
                name="retention",
                interval_seconds=60 * 60,
                lease_seconds=2 * 60 * 60,
                function=retention.purge_expired,
            )
        )
        report_task = asyncio.create_task(
            run_periodic_job(
                job_coordinator,
                name="scheduled-reports",
                interval_seconds=60,
                lease_seconds=30 * 60,
                function=report_delivery.deliver_due,
            )
        )
        timesheet_task = asyncio.create_task(
            run_periodic_job(
                job_coordinator,
                name="timesheet-generation",
                interval_seconds=6 * 60 * 60,
                lease_seconds=12 * 60 * 60,
                function=database.generate_open_timesheets,
            )
        )
        github_task = asyncio.create_task(
            run_periodic_job(
                job_coordinator,
                name="github-integration-sync",
                interval_seconds=60,
                lease_seconds=10 * 60,
                function=github.sync_due,
            )
        )
        tasks = [retention_task, report_task, timesheet_task, github_task]
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="jira-integration-sync",
                    interval_seconds=60,
                    lease_seconds=10 * 60,
                    function=jira.sync_due,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="payroll-reconciliation",
                    interval_seconds=5 * 60,
                    lease_seconds=15 * 60,
                    function=payroll_delivery.reconcile_due,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="slack-notification-delivery",
                    interval_seconds=15,
                    lease_seconds=10 * 60,
                    function=slack.deliver_due,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="slack-outbox-retention",
                    interval_seconds=24 * 60 * 60,
                    lease_seconds=2 * 60 * 60,
                    function=lambda: database.purge_slack_outbox(
                        datetime.now(UTC) - timedelta(days=settings.retention_days)
                    ),
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="asana-integration-sync",
                    interval_seconds=60,
                    lease_seconds=20 * 60,
                    function=asana.sync_due,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="asana-comment-sync",
                    interval_seconds=60,
                    lease_seconds=10 * 60,
                    function=asana.sync_due_comments,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="oauth-pending-cleanup",
                    interval_seconds=10 * 60,
                    lease_seconds=20 * 60,
                    function=database.purge_pending_oauth_credentials,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                run_periodic_job(
                    job_coordinator,
                    name="jira-worklog-sync",
                    interval_seconds=60,
                    lease_seconds=10 * 60,
                    function=jira.sync_due_worklogs,
                )
            )
        )
        if integration_credentials:
            tasks.append(
                asyncio.create_task(
                    run_periodic_job(
                        job_coordinator,
                        name="integration-credential-rewrap",
                        interval_seconds=60 * 60,
                        lease_seconds=2 * 60 * 60,
                        function=integration_credentials.rewrap_all,
                    )
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )
            await asyncio.to_thread(job_coordinator.release)
            github.close()
            jira.close()
            asana.close()
            slack.close()
            payroll_delivery.close()
            database.close()

    app = FastAPI(title="Dayfinch", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.storage = storage
    app.state.retention = retention
    app.state.report_delivery = report_delivery
    app.state.payroll_delivery = payroll_delivery
    app.state.invoice_vault = invoice_vault
    app.state.invitation_delivery = invitation_delivery
    app.state.oidc = oidc
    app.state.saml = saml
    app.state.github = github
    app.state.jira = jira
    app.state.asana = asana
    app.state.slack = slack
    app.state.integration_credentials = integration_credentials
    app.state.job_coordinator = job_coordinator
    app.state.metrics = metrics
    app.state.web = WebSecurity(database, settings.session_secret)
    app.state.timesheets = TimesheetService(database)
    templates = Jinja2Templates(directory=UI_DIR / "templates")
    asset_version = _asset_version()
    templates.env.globals["asset_version"] = asset_version
    app.state.templates = templates
    app.state.asset_version = asset_version
    app.state.dummy_password_hash = hash_password("invalid-password-for-timing-only")
    # Added before SessionMiddleware so Starlette places this inside the signed
    # session decoder; request buckets can then distinguish authenticated users.
    app.add_middleware(
        RequestRateLimitMiddleware,
        database=database,
        settings=settings,
        secret=settings.session_secret,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tracker_session",
        # OAuth/OIDC callbacks are top-level cross-site navigations. Lax cookies
        # preserve the signed session state while form mutations remain CSRF guarded.
        same_site="lax",
        https_only=settings.cookie_secure,
        max_age=8 * 60 * 60,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.environment == "production",
    )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker() -> Response:
        source = (UI_DIR / "static" / "service-worker.js").read_text()
        return Response(
            source.replace("__DAYFINCH_ASSET_VERSION__", asset_version),
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(projects_router)
    app.include_router(devices_router)
    app.include_router(integrations_router)
    app.include_router(reports_router)
    app.include_router(timesheets_router)
    app.include_router(scim_router)

    @app.exception_handler(SCIMError)
    async def handle_scim_error(_request, error: SCIMError):
        return scim_error_response(error)

    app.include_router(workforce_router)
    app.include_router(agent_api_router)

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/livez", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz", include_in_schema=False)
    def readiness() -> Response:
        try:
            database.ping()
        except Exception as exc:
            LOGGER.error(
                "readiness_check_failed",
                extra={"exception_type": type(exc).__name__},
            )
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(authorization: str = Header(default="")) -> Response:
        expected = settings.metrics_bearer_token
        supplied = authorization.removeprefix("Bearer ")
        if not expected or not secrets.compare_digest(supplied, expected):
            return Response(status_code=404)
        return Response(
            metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
