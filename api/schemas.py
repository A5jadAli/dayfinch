from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Heartbeat(BaseModel):
    platform: str = Field(min_length=1, max_length=80)
    # Optional for a rolling upgrade from agents older than 0.6. New agents always
    # send both and receive durable, idempotent offline reconstruction.
    event_id: UUID | None = None
    observed_at: datetime | None = None
    status: Literal["active", "paused", "stopped"] = "active"
    task_id: UUID | None = None
    project_id: UUID | None = None
    note: str = Field(default="", max_length=500)
    idle_seconds: int = Field(default=0, ge=0, le=86_400)
    heartbeat_interval_seconds: int = Field(default=60, ge=15, le=3_600)


class LocationPoint(BaseModel):
    event_id: UUID
    recorded_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float = Field(default=0, ge=0, le=100_000)
    event_type: Literal["position", "enter", "exit"] = "position"


class UsagePoint(BaseModel):
    event_id: UUID
    observed_at: datetime
    active_app: str = Field(default="", max_length=160)
    active_url: str = Field(default="", max_length=255)
    focused_seconds: int = Field(default=0, ge=0, le=3600)


class FieldTimerEvent(BaseModel):
    event_id: UUID
    observed_at: datetime
    action: Literal["start", "pause", "resume", "stop"]
    project_id: UUID | None = None
    task_id: UUID | None = None
