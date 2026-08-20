"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)
    invite_code: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChannelCreate(BaseModel):
    tg_channel_id: str = Field(min_length=1, max_length=64)
    username: str | None = None
    title: str | None = None
    account_id: int | None = None


class ChannelUpdate(BaseModel):
    """Reassign which collecting account is responsible for a channel.

    ``None`` hands it back to the workspace's default account rather than
    leaving it uncollected.
    """

    account_id: int | None = None


class ChannelOut(BaseModel):
    id: int
    tg_channel_id: str
    username: str | None
    title: str | None
    is_active: bool
    account_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TelegramAccountOut(BaseModel):
    """A collecting account. The session string is never exposed."""

    id: int
    label: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class LinkOut(BaseModel):
    id: int
    channel_id: int
    url: str
    domain: str
    category: str
    confidence: float
    classified_by: str
    is_favorite: bool
    raw_text: str | None
    created_at: datetime
    last_checked_at: datetime | None
    http_status: int | None
    is_alive: bool | None

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkOut]


class StatsResponse(BaseModel):
    total_links: int
    total_channels: int
    by_category: dict[str, int]
    top_domains: list[tuple[str, int]]


class LinkImportRequest(BaseModel):
    """A free-text paste; every URL inside it is extracted and classified."""

    text: str = Field(min_length=1, max_length=50_000)


class LinkImportResponse(BaseModel):
    found: int
    stored: int
    duplicates: int


class LinkCategoryUpdate(BaseModel):
    """Correct a classification the automatic tiers got wrong."""

    category: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class BulkDeleteRequest(BaseModel):
    """Filter identifying which links to delete; empty matches the whole workspace."""

    q: str | None = Field(default=None, max_length=300)
    category: str | None = None


class BulkRecategorizeRequest(BaseModel):
    q: str | None = Field(default=None, max_length=300)
    category: str | None = None
    new_category: str


class BulkResult(BaseModel):
    affected: int


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    expires_at: datetime
    is_current: bool
    ip_address: str | None = None
    user_agent: str | None = None


class SecurityActivityOut(BaseModel):
    """Failed-login summary for the caller's own account.

    Counts and a timestamp only — the individual attempt rows are not
    exposed, because the addresses in them are attacker-supplied and
    rendering them verbatim in a dashboard is an injection surface for no
    added value.
    """

    failed_attempts: int
    window_minutes: int
    lockout_threshold: int
    distinct_ip_count: int
    last_failed_at: datetime | None


class DeleteAccountRequest(BaseModel):
    """Deleting a workspace is irreversible, so it is password-gated.

    A stolen session cookie is enough to browse; it must not also be enough
    to destroy the collection.
    """

    current_password: str
    confirm: str = Field(description="must be the literal string DELETE")


class DeleteAccountResponse(BaseModel):
    deleted: dict[str, int]


class WorkspaceRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}
