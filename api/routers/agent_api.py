from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ..schemas import Heartbeat
from ..services.privacy import normalize_domain
from .dependencies import device_from_authorization

router = APIRouter(prefix="/api/v1", tags=["agent"])


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
            event_id=str(payload.event_id) if payload.event_id else None,
            observed_at=observed,
            idle_seconds=payload.idle_seconds,
            heartbeat_interval_seconds=payload.heartbeat_interval_seconds,
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
) -> dict[str, str]:
    database = request.app.state.database
    storage = request.app.state.storage
    settings = request.app.state.settings
    try:
        parsed_id = str(uuid.UUID(record_id))
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid record id or timestamp"
        ) from exc

    if database.record_exists(parsed_id):
        return {"status": "duplicate", "record_id": parsed_id}

    session = database.get_work_session(session_id) if session_id else None
    if session and session["device_id"] != device["id"]:
        raise HTTPException(status_code=422, detail="Session does not belong to device")
    # captured_at, not an old in-memory session id, is authoritative after an
    # offline task switch or process restart.
    session = database.get_work_session_for_capture(device["id"], captured)

    data = await screenshot_file.read(settings.max_upload_bytes + 1)
    if not data or len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Screenshot is empty or too large")
    try:
        stored = await asyncio.to_thread(
            storage.save, device["id"], parsed_id, captured, data
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
            "active_app": active_app.strip()[:160] or None,
            "agent_version": agent_version,
            "screenshot_path": stored.key,
            "storage_version_id": stored.version_id,
            "focused_seconds": focused_seconds,
            # Suspected synthetic input never counts as genuine interaction, whatever
            # the agent reported, so faking activity cannot inflate the number.
            "interactive_seconds": 0
            if automation_suspected
            else min(interactive_seconds, focused_seconds),
            "active_url": normalize_domain(active_url) or None,
            "automation_suspected": automation_suspected,
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
