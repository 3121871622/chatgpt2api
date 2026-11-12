from __future__ import annotations

from pydantic import BaseModel, Field


class LocalBackupConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=720)
    keep: int | None = Field(default=None, ge=1, le=365)


class LocalBackupRunRequest(BaseModel):
    pass
