"""Pydantic request/response schemas.

Every schema here carries a worked ``examples`` entry, surfaced in the
OpenAPI document and in ``/docs`` (idea 110). Two rules make them worth
having rather than decorative:

- ``tests/test_api_maturity.py`` requires *every* model in this module to
  declare at least one example, so a new schema cannot ship without one.
- The same test feeds each example back through its own model. An example
  that stops matching its schema fails the build instead of quietly
  becoming documentation that lies.

Shared examples are module constants rather than copies, so a composite
schema and the schema it nests show the same values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


def _config(*examples: dict[str, Any], from_attributes: bool = False) -> ConfigDict:
    """Attach OpenAPI examples, optionally alongside ORM-mode loading."""
    config: ConfigDict = {"json_schema_extra": {"examples": list(examples)}}
    if from_attributes:
        config["from_attributes"] = True
    return config


# --- Worked examples -------------------------------------------------
# Realistic values, not placeholders: an example reading "string" teaches
# a client nothing it could not read off the type. Shared by the composite
# schemas below so a list and its items never show different shapes.

_CHANNEL_EXAMPLE: dict[str, Any] = {
    "id": 12,
    "tg_channel_id": "-1001234567890",
    "username": "python_weekly",
    "title": "Python Weekly",
    "is_active": True,
    "account_id": 3,
    "created_at": "2026-03-01T09:15:00",
}

_ACCOUNT_EXAMPLE: dict[str, Any] = {
    "id": 3,
    "label": "main collector",
    "is_active": False,
    "created_at": "2026-02-14T08:00:00",
    "last_success_at": "2026-08-18T02:00:11",
    "last_failure_at": "2026-08-20T02:00:07",
    "last_error": "AuthKeyUnregisteredError: the session was revoked",
    "consecutive_failures": 3,
    "disabled_reason": "3 consecutive failures; last error: AuthKeyUnregisteredError",
    "links_collected": 4127,
    "channel_count": 6,
}

_LINK_EXAMPLE: dict[str, Any] = {
    "id": 8412,
    "channel_id": 12,
    "url": "https://peps.python.org/pep-0703/",
    "domain": "peps.python.org",
    "category": "programming",
    "confidence": 0.92,
    "classified_by": "rules",
    "is_favorite": True,
    "matched_rule": "domain:peps.python.org",
    "source_type": "channel",
    "forwarded_from": None,
    "language": "en",
    "raw_text": "PEP 703 draft is worth a read this week",
    "created_at": "2026-08-11T19:04:22",
    "last_checked_at": "2026-08-19T03:12:40",
    "http_status": 200,
    "is_alive": True,
    "last_alive_at": "2026-08-19T03:12:40",
    "consecutive_failures": 0,
    "is_archived": False,
    "is_pinned": True,
    "notes": "read before the meeting",
    "click_count": 4,
    "status_category": "ok",
}

_FEEDBACK_EXAMPLE: dict[str, Any] = {
    "id": 57,
    "link_id": 8412,
    "url": "https://peps.python.org/pep-0703/",
    "previous_category": "other",
    "new_category": "programming",
    "previous_confidence": 0.4,
    "previous_matched_rule": None,
    "created_at": "2026-08-12T07:30:00",
}

_VITALITY_EXAMPLE: dict[str, Any] = {
    "alive": 7413,
    "dead": 289,
    "unchecked": 1102,
    "archived": 64,
    "deadest_domains": [["short.link", 71], ["files.example.com", 33]],
}

_COLLECTION_EXAMPLE: dict[str, Any] = {
    "last_run_at": "2026-08-20T02:00:11",
    "hours_since_last_run": 5.4,
    "looks_stalled": False,
}

_STORAGE_EXAMPLE: dict[str, Any] = {
    "database_bytes": 41_385_984,
    "link_count": 8804,
    "largest_table": "links",
}


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)
    invite_code: str | None = None

    model_config = _config(
        {
            "email": "sara@example.com",
            "password": "correct-horse-battery-staple",
            "workspace_name": "Research links",
            "invite_code": None,
        }
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    # Optional so that every existing client keeps working unchanged: an
    # account without a second factor never needs it, and one with it is
    # told so explicitly rather than by a bare 401.
    totp_code: str | None = Field(default=None, max_length=32)

    model_config = _config(
        {"email": "sara@example.com", "password": "correct-horse-battery-staple"},
        {"email": "sara@example.com", "password": "correct-horse-battery-staple", "totp_code": "123456"},
    )


class ChannelCreate(BaseModel):
    tg_channel_id: str = Field(min_length=1, max_length=64)
    username: str | None = None
    title: str | None = None
    account_id: int | None = None

    model_config = _config(
        {"tg_channel_id": "-1001234567890", "username": "python_weekly", "title": "Python Weekly", "account_id": 3}
    )


class ChannelUpdate(BaseModel):
    """Reassign which collecting account is responsible for a channel.

    ``None`` hands it back to the workspace's default account rather than
    leaving it uncollected.
    """

    account_id: int | None = None

    model_config = _config({"account_id": 3}, {"account_id": None})


class ChannelOut(BaseModel):
    id: int
    tg_channel_id: str
    username: str | None
    title: str | None
    is_active: bool
    account_id: int | None
    created_at: datetime

    model_config = _config(_CHANNEL_EXAMPLE, from_attributes=True)


class TelegramAccountOut(BaseModel):
    """A collecting account and its health. The session string is never exposed."""

    id: int
    label: str
    is_active: bool
    created_at: datetime
    # Two timestamps, not one "last run": a failed run says nothing about
    # when the account last actually worked, which is the question you ask
    # when deciding whether it needs re-authorising.
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    # Set only when the system disabled the account itself. NULL on one a
    # human disabled — the two need different responses, and a single
    # is_active boolean cannot say which happened.
    disabled_reason: str | None
    links_collected: int
    channel_count: int

    model_config = _config(_ACCOUNT_EXAMPLE, from_attributes=True)


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

    model_config = _config(_LINK_EXAMPLE, from_attributes=True)


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

    model_config = _config(_FEEDBACK_EXAMPLE, from_attributes=True)


class FeedbackListResponse(BaseModel):
    total: int
    items: list[ClassificationFeedbackOut]

    model_config = _config({"total": 1, "items": [_FEEDBACK_EXAMPLE]})


class SavedSearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # The search endpoint's query parameters, as given. Validated against a
    # known key set server-side rather than trusted, so a saved search
    # cannot smuggle an arbitrary parameter into a later request.
    filters: dict[str, str]

    model_config = _config(
        {"name": "dead python links", "filters": {"q": "python", "category": "programming", "alive": "false"}}
    )


class SavedSearchOut(BaseModel):
    id: int
    name: str
    filters: dict[str, str]
    created_at: datetime

    model_config = _config(
        {
            "id": 4,
            "name": "dead python links",
            "filters": {"q": "python", "category": "programming", "alive": "false"},
            "created_at": "2026-07-02T11:20:00",
        }
    )


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkOut]

    model_config = _config({"total": 8804, "page": 1, "page_size": 25, "items": [_LINK_EXAMPLE]})


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

    model_config = _config(_VITALITY_EXAMPLE)


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

    model_config = _config(_COLLECTION_EXAMPLE)


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

    model_config = _config(
        {
            "total_links": 8804,
            "total_channels": 6,
            "by_category": {"programming": 3120, "news": 2044, "other": 3640},
            "top_domains": [["github.com", 812], ["youtube.com", 401]],
            "added_this_week": 137,
            "added_this_month": 611,
            "vitality": _VITALITY_EXAMPLE,
            "collection": _COLLECTION_EXAMPLE,
            "storage": _STORAGE_EXAMPLE,
        }
    )


class LinkImportRequest(BaseModel):
    """A free-text paste; every URL inside it is extracted and classified."""

    text: str = Field(min_length=1, max_length=50_000)

    model_config = _config(
        {"text": "Two worth keeping: https://peps.python.org/pep-0703/ and https://docs.astral.sh/ruff/"}
    )


class LinkImportResponse(BaseModel):
    found: int
    stored: int
    duplicates: int

    model_config = _config({"found": 2, "stored": 1, "duplicates": 1})


class LinkCategoryUpdate(BaseModel):
    """Correct a classification the automatic tiers got wrong."""

    category: str

    model_config = _config({"category": "programming"})


class LinkNotesUpdate(BaseModel):
    """A user's own note about a link. Empty string clears it."""

    notes: str = Field(max_length=2000)

    model_config = _config({"notes": "read before the meeting"}, {"notes": ""})


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

    model_config = _config(_STORAGE_EXAMPLE)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)

    model_config = _config(
        {"current_password": "correct-horse-battery-staple", "new_password": "a-different-long-one"}
    )


class BulkDeleteRequest(BaseModel):
    """Filter identifying which links to delete; empty matches the whole workspace."""

    q: str | None = Field(default=None, max_length=300)
    category: str | None = None

    model_config = _config({"q": "webinar", "category": "other"})


class BulkRecategorizeRequest(BaseModel):
    q: str | None = Field(default=None, max_length=300)
    category: str | None = None
    new_category: str

    model_config = _config({"q": "pep", "category": "other", "new_category": "programming"})


class BulkResult(BaseModel):
    affected: int

    model_config = _config({"affected": 42})


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    expires_at: datetime
    is_current: bool
    ip_address: str | None = None
    user_agent: str | None = None

    model_config = _config(
        {
            "id": 91,
            "created_at": "2026-08-20T06:41:00",
            "expires_at": "2026-09-03T06:41:00",
            "is_current": True,
            "ip_address": "203.0.113.24",
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64)",
        }
    )


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

    model_config = _config(
        {
            "failed_attempts": 2,
            "window_minutes": 15,
            "lockout_threshold": 5,
            "distinct_ip_count": 1,
            "last_failed_at": "2026-08-20T06:38:12",
        }
    )


class DeleteAccountRequest(BaseModel):
    """Deleting a workspace is irreversible, so it is password-gated.

    A stolen session cookie is enough to browse; it must not also be enough
    to destroy the collection.
    """

    current_password: str
    confirm: str = Field(description="must be the literal string DELETE")

    model_config = _config({"current_password": "correct-horse-battery-staple", "confirm": "DELETE"})


class DeleteAccountResponse(BaseModel):
    deleted: dict[str, int]

    model_config = _config({"deleted": {"links": 8804, "channels": 6, "telegram_accounts": 1}})


class WorkspaceRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    model_config = _config({"name": "Research links"})


class AccountSummary(BaseModel):
    """Everything the dashboard header needs, in one round trip.

    Deliberately an *aggregate*, not a new source of truth: every number
    here is produced by the same function that serves it individually
    (``storage.link_count``, ``list_active_sessions``,
    ``recent_failed_attempts``), and a test asserts each field equals what
    its own endpoint returns. A summary that can disagree with the pages
    it summarises is worse than making the extra calls.
    """

    user_id: int
    email: str
    workspace_id: int
    workspace_name: str | None
    member_since: datetime
    total_links: int
    total_channels: int
    # Collecting accounts split by whether they can still work. Two counts
    # rather than one total: "3 accounts" reads as healthy when two of
    # them are disabled, which is the case that needs attention.
    active_accounts: int
    disabled_accounts: int
    active_sessions: int
    failed_logins_recent: int

    model_config = _config(
        {
            "user_id": 1,
            "email": "sara@example.com",
            "workspace_id": 1,
            "workspace_name": "Research links",
            "member_since": "2026-02-14T07:55:00",
            "total_links": 8804,
            "total_channels": 6,
            "active_accounts": 1,
            "disabled_accounts": 1,
            "active_sessions": 2,
            "failed_logins_recent": 0,
        }
    )


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    model_config = _config({"name": "obsidian sync script"})


class ApiKeyOut(BaseModel):
    """A key as it can be shown *after* creation — without the key itself."""

    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    use_count: int

    model_config = _config(
        {
            "id": 7,
            "name": "obsidian sync script",
            "prefix": "lipk_A7bQ2f",
            "created_at": "2026-08-21T09:40:00",
            "last_used_at": "2026-08-21T10:02:14",
            "use_count": 38,
        },
        from_attributes=True,
    )


class ApiKeyCreated(BaseModel):
    """The one and only response that carries the raw key.

    ``key`` is not stored anywhere in recoverable form, so this response is
    the single opportunity to copy it. Said in the field description too,
    because a caller who assumes they can fetch it later finds out at the
    worst moment.
    """

    key: str = Field(description="the raw key — shown once, never retrievable again")
    api_key: ApiKeyOut

    model_config = _config(
        {
            "key": "lipk_A7bQ2fEXAMPLEONLYnotarealkeyvalue0000000000",
            "api_key": {
                "id": 7,
                "name": "obsidian sync script",
                "prefix": "lipk_A7bQ2f",
                "created_at": "2026-08-21T09:40:00",
                "last_used_at": None,
                "use_count": 0,
            },
        }
    )


class TotpStatus(BaseModel):
    enabled: bool
    # Zero remaining while enabled is the state worth warning about: the
    # second factor still applies and there is no longer any way past a
    # lost authenticator.
    recovery_codes_remaining: int

    model_config = _config({"enabled": True, "recovery_codes_remaining": 8})


class TotpSetupResponse(BaseModel):
    """Shown once during setup. Nothing here is retrievable afterwards."""

    secret: str = Field(description="base32 secret — for manual entry if the URI cannot be scanned")
    otpauth_uri: str = Field(description="paste or scan into an authenticator app")

    model_config = _config(
        {
            "secret": "JBSWY3DPEHPK3PXP",
            "otpauth_uri": "otpauth://totp/Link%20Intelligence:sara@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Link+Intelligence",
        }
    )


class TotpCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)

    model_config = _config({"code": "123456"})


class TotpDisableRequest(BaseModel):
    """Turning the second factor off is a downgrade, so it is password-gated."""

    current_password: str

    model_config = _config({"current_password": "correct-horse-battery-staple"})


class RecoveryCodesResponse(BaseModel):
    """The plaintext codes, returned once. Only hashes are stored."""

    recovery_codes: list[str]

    model_config = _config({"recovery_codes": ["a1b2c3d4-e5f6a7b8", "1122aabb-33cc44dd"]})


class AlertPreferenceOut(BaseModel):
    """One alert type and whether it is on for this workspace."""

    key: str
    label: str
    description: str
    enabled: bool
    # Whether this is the type's default or an explicit choice. Shown
    # because "on because I chose it" and "on because it ships that way"
    # are different, and only the second is worth revisiting.
    is_default: bool

    model_config = _config(
        {
            "key": "weekly_digest",
            "label": "ملخّص أسبوعي",
            "description": "روابط جديدة، روابط ماتت، وقنوات صامتة خلال الأسبوع",
            "enabled": False,
            "is_default": True,
        }
    )


class AlertPreferenceUpdate(BaseModel):
    enabled: bool

    model_config = _config({"enabled": True})


class NotificationOut(BaseModel):
    id: int
    alert_type: str
    title: str
    body: str
    # Zero is normal (no bot linked) rather than an error — but it makes
    # "raised but never delivered" visible, which is otherwise invisible.
    delivered_count: int
    read_at: datetime | None
    created_at: datetime

    model_config = _config(
        {
            "id": 12,
            "alert_type": "collector_failed",
            "title": "توقّف الجامع",
            "body": "كل حسابات الجمع أخفقت في آخر تشغيلة رغم وجود ٦ قنوات نشطة.",
            "delivered_count": 1,
            "read_at": None,
            "created_at": "2026-08-21T02:00:11",
        },
        from_attributes=True,
    )


class NotificationListResponse(BaseModel):
    total: int
    unread: int
    items: list[NotificationOut]

    model_config = _config(
        {
            "total": 34,
            "unread": 2,
            "items": [
                {
                    "id": 12,
                    "alert_type": "collector_failed",
                    "title": "توقّف الجامع",
                    "body": "كل حسابات الجمع أخفقت في آخر تشغيلة رغم وجود ٦ قنوات نشطة.",
                    "delivered_count": 1,
                    "read_at": None,
                    "created_at": "2026-08-21T02:00:11",
                }
            ],
        }
    )


class WorkflowRunReport(BaseModel):
    """What a finished workflow posts about itself."""

    name: str = Field(min_length=1, max_length=100)
    conclusion: str = Field(min_length=1, max_length=30)
    detail: str | None = Field(default=None, max_length=500)
    commit_sha: str | None = Field(default=None, max_length=40)
    duration_seconds: int | None = Field(default=None, ge=0, le=86_400)

    model_config = _config(
        {
            "name": "collector",
            "conclusion": "success",
            "detail": "37 new link(s) across 6 channel(s)",
            "commit_sha": "d97841c733f29358f242798e89270a389ca5201b",
            "duration_seconds": 74,
        }
    )


class WorkflowRunOut(BaseModel):
    name: str
    conclusion: str
    detail: str | None
    commit_sha: str | None
    duration_seconds: int | None
    started_at: datetime

    model_config = _config(
        {
            "name": "collector",
            "conclusion": "success",
            "detail": "37 new link(s) across 6 channel(s)",
            "commit_sha": "d97841c733f29358f242798e89270a389ca5201b",
            "duration_seconds": 74,
            "started_at": "2026-08-21T02:00:11",
        },
        from_attributes=True,
    )


class SystemStatus(BaseModel):
    """The operator's one screen: what is deployed, and is it healthy.

    Every counter here describes the **current process**. The free tier
    sleeps after inactivity, so these restart — the uptime is reported
    beside them precisely so that window is visible rather than implied.
    """

    deploy_commit: str | None
    service_name: str | None
    schema_version: str | None
    process_uptime_seconds: float
    requests_since_start: int
    server_errors_since_start: int
    mean_response_ms: float
    median_response_ms: float
    p95_response_ms: float
    slowest_response_ms: float
    sampled_requests: int
    # Newest run per workflow, so a stopped job is visible by its absence
    # or its age rather than by scrolling a log.
    latest_runs: list[WorkflowRunOut]

    model_config = _config(
        {
            "deploy_commit": "d97841c733f29358f242798e89270a389ca5201b",
            "service_name": "link-intel-web",
            "schema_version": "0017_workflow_runs",
            "process_uptime_seconds": 3821.4,
            "requests_since_start": 1044,
            "server_errors_since_start": 0,
            "mean_response_ms": 18.2,
            "median_response_ms": 9.1,
            "p95_response_ms": 61.0,
            "slowest_response_ms": 412.7,
            "sampled_requests": 512,
            "latest_runs": [
                {
                    "name": "collector",
                    "conclusion": "success",
                    "detail": "37 new link(s) across 6 channel(s)",
                    "commit_sha": "d97841c733f29358f242798e89270a389ca5201b",
                    "duration_seconds": 74,
                    "started_at": "2026-08-21T02:00:11",
                }
            ],
        }
    )


class WorkspaceOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    model_config = _config(
        {"id": 1, "name": "Research links", "created_at": "2026-02-14T07:55:00"}, from_attributes=True
    )
