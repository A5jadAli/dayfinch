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
    idle_seconds: int = Field(default=0, ge=0, le=86_400)
    heartbeat_interval_seconds: int = Field(default=60, ge=15, le=3_600)
