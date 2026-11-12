from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GovernanceConfigRequest(BaseModel):
    enabled: bool | None = None
    mode: Literal["off", "report", "disable", "purge"] | None = None
    run_every_hours: int | None = Field(default=None, ge=1, le=720)
    account_log_retention_hours: int | None = Field(default=None, ge=1, le=8760)
    max_per_run: int | None = Field(default=None, ge=1, le=1_000_000)


class GovernanceRunRequest(BaseModel):
    dry_run: bool = False
    mode: Literal["off", "report", "disable", "purge"] | None = None
    confirm: bool = False
