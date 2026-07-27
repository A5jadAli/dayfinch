from pydantic import BaseModel, Field


class Heartbeat(BaseModel):
    platform: str = Field(min_length=1, max_length=80)
    status: str = Field(default="active", min_length=1, max_length=40)
    task_id: str | None = Field(default=None, max_length=40)
