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
    # Why this category was chosen, e.g. "extension:pdf". Nullable because
    # links stored before the column existed have no recorded reason.
    matched_rule: str | None
    source_type: str
    forwarded_from: str | None
    language: str | None
    raw_text: str | None
    created_at: datetime
    last_checked_at: datetime | None
    http_status: int | None
    is_alive: bool | None
    last_alive_at: datetime | None
    consecutive_failures: int
    is_archived: bool
    is_pinned: bool
    notes: str | None
    click_count: int
    # A stable label ("ok", "redirect", "blocked", "missing", "throttled",
    # "server_error", "unreachable", "unchecked") derived from the two
    # fields above. Computed server-side so every client agrees on what a
    # 403 means, and machine-readable so it can be filtered rather than
    # parsed out of prose.
    status_category: str

    model_config = {"from_attributes": True}


class ClassificationFeedbackOut(BaseModel):
    """One recorded correction: what the classifier said, and what a human said."""

    id: int
    link_id: int
    url: str
    previous_category: str
    new_category: str
    previous_confidence: float
    previous_matched_rule: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedbackListResponse(BaseModel):
    total: int
    items: list[ClassificationFeedbackOut]


class SavedSearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # The search endpoint's query parameters, as given. Validated against a
    # known key set server-side rather than trusted, so a saved search
    # cannot smuggle an arbitrary parameter into a later request.
    filters: dict[str, str]


class SavedSearchOut(BaseModel):
    id: int
    name: str
    filters: dict[str, str]
    created_at: datetime


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkOut]


class VitalityStats(BaseModel):
    """The alive/dead/unchecked split, plus where the rot is concentrated."""

    alive: int
    dead: int
    unchecked: int
    archived: int
    # Domains with the most dead links, worst first. A source that mostly
    # posts links that stop working is a quality signal about the source,
    # which is not visible one link at a time.
    deadest_domains: list[tuple[str, int]]


class CollectionHealth(BaseModel):
    """Whether the scheduled collector is still running at all.

    A stopped collector is the failure mode with no symptom: the dashboard
    keeps working, search keeps working, and the collection simply stops
    growing. Nothing on screen said so until this existed.
    """

    last_run_at: datetime | None
    hours_since_last_run: float | None
    # True once the gap exceeds the expected schedule by a wide margin.
    # Advisory, not an alarm: a workspace that has never run the collector
    # (manual-only use) is not unhealthy, so this stays False for it.
    looks_stalled: bool


class StatsResponse(BaseModel):
    total_links: int
    total_channels: int
    by_category: dict[str, int]
    top_domains: list[tuple[str, int]]
    added_this_week: int
    added_this_month: int
    vitality: VitalityStats
    collection: CollectionHealth
    storage: StorageStats


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


class LinkNotesUpdate(BaseModel):
    """A user's own note about a link. Empty string clears it."""

    notes: str = Field(max_length=2000)


class StorageStats(BaseModel):
    """How much of the free tier's room is used.

    Shown because the plan this runs on is size-limited and the failure
    mode is writes starting to fail — which is worth seeing coming rather
    than discovering. ``database_bytes`` is None on SQLite and on any
    Postgres role without permission to read the size.
    """

    database_bytes: int | None
    link_count: int
    largest_table: str | None


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
