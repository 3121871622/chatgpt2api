from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DebugLogConfigRequest(BaseModel):
    enabled: bool | None = None
    mode: Literal["time", "size"] | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    max_size_mb: int | None = Field(default=None, ge=16, le=65536)
    slack_mb: int | None = Field(default=None, ge=0, le=32768)
    level: Literal["debug", "info"] | None = None
