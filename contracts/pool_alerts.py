from __future__ import annotations

from pydantic import BaseModel, Field


class PoolAlertConfigRequest(BaseModel):
    enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=1024)
    min_usable: int | None = Field(default=None, ge=0, le=1_000_000)
    min_refresh_ratio_percent: int | None = Field(default=None, ge=0, le=100)
    check_interval_seconds: int | None = Field(default=None, ge=300, le=86_400)
    cooldown_seconds: int | None = Field(default=None, ge=600, le=7 * 86_400)


class PoolAlertRunRequest(BaseModel):
    dry_run: bool = False
