from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AuthRole = Literal["admin", "user", "unknown"]
AuthHomeRoute = Literal["/login", "/", "/admin", "/studio"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthSubject(_StrictModel):
    id: str
    name: str
    role: AuthRole


class AuthCapabilities(_StrictModel):
    admin_console: bool = False
    studio: bool = False


class AuthView(_StrictModel):
    schema_version: Literal[1] = 1
    authenticated: bool
    version: str
    subject: AuthSubject | None
    capabilities: AuthCapabilities = Field(default_factory=AuthCapabilities)
    home_route: AuthHomeRoute
    session_token: str | None = None

    @model_validator(mode="after")
    def validate_session_shape(self) -> "AuthView":
        if self.authenticated:
            if self.subject is None:
                raise ValueError("authenticated sessions require a subject")
            if self.home_route == "/login":
                raise ValueError("authenticated sessions require an authenticated home route")
        else:
            if self.subject is not None:
                raise ValueError("anonymous sessions must not expose a subject")
            if self.capabilities.admin_console or self.capabilities.studio:
                raise ValueError("anonymous sessions must not expose capabilities")
            if self.home_route != "/login":
                raise ValueError("anonymous sessions must use the login route")
        if self.capabilities.admin_console and self.subject and self.subject.role != "admin":
            raise ValueError("admin console capability requires the admin role")
        return self


class UserKeyCreateRequest(_StrictModel):
    name: str = ""
    image_daily_limit: int = 0
    image_monthly_limit: int = 0
    expires_at: str | None = None


class UserKeyUpdateRequest(_StrictModel):
    name: str | None = None
    enabled: bool | None = None
    key: str | None = None
    image_daily_limit: int | None = None
    image_monthly_limit: int | None = None
    expires_at: str | None = None


class UserKeyBatchUpdateRequest(_StrictModel):
    ids: list[str] = Field(default_factory=list)
    enabled: bool | None = None
    image_daily_limit: int | None = None
    image_monthly_limit: int | None = None
    expires_at: str | None = None
    delete: bool = False


class UserKeyView(_StrictModel):
    id: str
    name: str
    role: Literal["admin", "user"]
    enabled: bool
    created_at: str | None = None
    last_used_at: str | None = None
    image_daily_limit: int = 0
    image_monthly_limit: int = 0
    image_daily_used: int = 0
    image_monthly_used: int = 0
    image_quota_day: str = ""
    image_quota_month: str = ""
    expires_at: str | None = None


class UserKeyListView(_StrictModel):
    items: list[UserKeyView] = Field(default_factory=list)


class UserKeyCreateResult(_StrictModel):
    item: UserKeyView
    raw_key: str


class UserKeyUpdateResult(_StrictModel):
    item: UserKeyView


class UserKeyDeleteResult(_StrictModel):
    deleted_id: str
