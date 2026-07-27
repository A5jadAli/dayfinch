from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .database import Database
from .routers.agent_api import router as agent_api_router
from .routers.auth import router as auth_router
from .routers.dashboard import router as dashboard_router
from .routers.devices import router as devices_router
from .routers.projects import router as projects_router
from .routers.reports import router as reports_router
from .security import hash_password
from .services.retention import RetentionService, run_retention_worker
from .storage import create_storage
from .web import WebSecurity


PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    database = Database(settings.database_path)
    storage = create_storage(settings)
    retention = RetentionService(database, storage, settings.retention_days)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        database.bootstrap_admin(
            settings.admin_email, hash_password(settings.admin_password)
        )
        retention.purge_expired()
        task = asyncio.create_task(run_retention_worker(retention))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Dayfinch", version="0.3.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.storage = storage
    app.state.retention = retention
    app.state.web = WebSecurity(database)
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.dummy_password_hash = hash_password("invalid-password-for-timing-only")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tracker_session",
        same_site="strict",
        https_only=settings.cookie_secure,
        max_age=8 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(projects_router)
    app.include_router(devices_router)
    app.include_router(reports_router)
    app.include_router(agent_api_router)

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("tracker_server.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
