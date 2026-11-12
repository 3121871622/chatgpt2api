from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LegacySyncConfigRequest(BaseModel):
    enabled: bool | None = None
    paths: list[str] | None = Field(default=None, max_length=20)
    interval_seconds: int | None = Field(default=None, ge=60, le=86_400)
    sync_accounts: bool | None = None
    sync_settings: Literal["off", "fill", "overwrite"] | None = None
    sync_account_groups: bool | None = None
    sync_call_records: bool | None = None
    max_call_records_per_run: int | None = Field(default=None, ge=0, le=50_000)
    auto_check: bool | None = None
    check_per_run: int | None = Field(default=None, ge=0, le=5_000)

    @field_validator("paths")
    @classmethod
    def _clean_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned


class LegacySyncRunRequest(BaseModel):
    dry_run: bool = False
