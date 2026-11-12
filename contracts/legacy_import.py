from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LegacyAccountSelection(BaseModel):
    enabled: bool = True
    mode: Literal["all", "selected"] = "all"
    account_ids: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)


class LegacySettingsSelection(BaseModel):
    enabled: bool = True
    mode: Literal["fill", "overwrite"] = "fill"
    keys: list[str] = Field(default_factory=list)


class LegacyImportOptions(BaseModel):
    accounts: LegacyAccountSelection | None = None
    settings: LegacySettingsSelection | None = None
    account_groups: bool = True
    call_records: bool = False
    max_call_records: int = Field(default=2000, ge=0, le=20000)


class LegacyImportRequest(BaseModel):
    options: LegacyImportOptions = Field(default_factory=LegacyImportOptions)
    dry_run: bool = False
    confirm: bool = False


class DebugLogClearRequest(BaseModel):
    confirm: bool = False
