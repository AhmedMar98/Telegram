"""Self-service data export and account deletion.

Both operations answer the same question — "what does this platform hold
about my workspace?" — from opposite ends, so they share one definition of
which tables that is. ``WORKSPACE_TABLES`` is that definition, and
``tests/test_account_data.py`` asserts it against SQLAlchemy's own metadata
so a table added later cannot quietly escape either operation.

Deleting is genuinely destructive and irreversible: it removes the
workspace row itself, every user in it, and every collected link. It is
not a soft delete and there is no undo, which is why the endpoint requires
the caller's current password.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Delete, delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import (
    ActionEvent,
    ApiKey,
    AuditLog,
    AuthSession,
    Beneficiary,
    BotLink,
    BotLinkCode,
    Channel,
    ClassificationFeedback,
    CollectionRun,
    CoverageSnapshot,
    Evidence,
    JoinRequest,
    KeywordRule,
    Lead,
    Link,
    LoginAttempt,
    Message,
    Notification,
    NotificationPreference,
    Occurrence,
    Resource,
    SavedSearch,
    SourceAccess,
    SourceAssignment,
    SourceEvent,
    SourceProgress,
    TelegramAccount,
    User,
    WorkflowRun,
    Workspace,
)

# Every model carrying a ``workspace_id``, child-first so that deleting in
# this order never trips a foreign key. Three further tables key on
# something else and are handled separately in ``delete_workspace``:
# AuthSession (user_id), LoginAttempt (email) and ActionEvent (scope +
# identifier).
WORKSPACE_TABLES = (
    ApiKey,
    WorkflowRun,
    # Telemetry about the workspace's own collection: it names how many
    # sources it has and how many are failing, so it leaves with it.
    CoverageSnapshot,
    # Leads before beneficiaries, and beneficiaries before channels: a lead
    # points at both, so deleting either first leaves a dangling reference
    # on any engine that enforces the foreign key.
    Lead,
    Beneficiary,
    KeywordRule,
    Notification,
    NotificationPreference,
    ClassificationFeedback,
    SavedSearch,
    # The target source model, ordered by what points at what. An
    # occurrence points at a resource, a message and a channel, so it goes
    # before all three; a resource points at nothing but the workspace, so
    # it only has to precede that.
    Occurrence,
    Link,
    Resource,
    # After Link and Occurrence, before Channel: a link points at its
    # message, an occurrence points at both, and a message points at its
    # channel, so this is the only order in which no step leaves a dangling
    # reference on an engine that enforces them.
    Message,
    # Access, assignment and event rows all point at a channel, an account
    # or an evidence row, so they come before every one of those. Evidence
    # is last of the four for the same reason: the other three cite it.
    SourceAccess,
    SourceAssignment,
    SourceEvent,
    # After the three above and before Evidence: a join request points at a
    # channel, an account and an evidence row, so it precedes all three.
    JoinRequest,
    # The collection runtime's own rows. A run points at a channel, an
    # account and an evidence row; progress points at a channel. Both
    # therefore precede all three, and precede Evidence for the same
    # reason the four above do.
    CollectionRun,
    SourceProgress,
    Evidence,
    AuditLog,
    BotLink,
    BotLinkCode,
    Channel,
    TelegramAccount,
    User,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _masked_webhook(workspace: Workspace) -> str | None:
    from app.webhook import configured_url, mask

    url = configured_url(workspace)
    return mask(url) if url else None


def export_workspace(db: Session, workspace_id: int) -> dict[str, Any]:
    """Everything the platform holds for one workspace, as plain JSON.

    Credentials are deliberately transformed rather than copied: password
    hashes and Telegram session strings are omitted, and session tokens are
    never stored in recoverable form to begin with. An export is a
    portability tool, not a way to lift secrets out of the database in
    plaintext.
    """
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"no workspace with id {workspace_id}")

    def rows(model):
        return db.query(model).filter(model.workspace_id == workspace_id).all()

    return {
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "created_at": _iso(workspace.created_at),
            # Masked, not omitted and not copied. An incoming-webhook URL
            # carries a secret token in its path, which puts it in the same
            # class as the session strings above; but unlike those, whether
            # one is configured is a fact worth carrying to a new
            # deployment, so the fact travels and the credential does not.
            "webhook": _masked_webhook(workspace),
        },
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "created_at": _iso(u.created_at),
            }
            for u in rows(User)
        ],
        "telegram_accounts": [
            # session_string is omitted on purpose — see the docstring.
            {"id": a.id, "label": a.label, "is_active": a.is_active, "created_at": _iso(a.created_at)}
            for a in rows(TelegramAccount)
        ],
        "channels": [
            {
                "id": c.id,
                "tg_channel_id": c.tg_channel_id,
                "username": c.username,
                "title": c.title,
                "account_id": c.account_id,
                "last_message_id": c.last_message_id,
                "is_active": c.is_active,
                "created_at": _iso(c.created_at),
            }
            for c in rows(Channel)
        ],
        "links": [
            {
                "id": link.id,
                "channel_id": link.channel_id,
                "message_id": link.message_id,
                "url": link.url,
                "domain": link.domain,
                "category": link.category,
                "confidence": link.confidence,
                "classified_by": link.classified_by,
                "matched_rule": link.matched_rule,
                "source_type": link.source_type,
                "forwarded_from": link.forwarded_from,
                "language": link.language,
                "is_favorite": link.is_favorite,
                "raw_text": link.raw_text,
                "is_alive": link.is_alive,
                "http_status": link.http_status,
                "last_checked_at": _iso(link.last_checked_at),
                "posted_at": _iso(link.posted_at),
                "created_at": _iso(link.created_at),
            }
            for link in rows(Link)
        ],
        "classification_feedback": [
            {
                "id": f.id,
                "link_id": f.link_id,
                "url": f.url,
                "previous_category": f.previous_category,
                "new_category": f.new_category,
                "previous_confidence": f.previous_confidence,
                "previous_matched_rule": f.previous_matched_rule,
                "created_at": _iso(f.created_at),
            }
            for f in rows(ClassificationFeedback)
        ],
        "saved_searches": [
            {"id": q.id, "name": q.name, "filters": q.filters, "created_at": _iso(q.created_at)}
            for q in rows(SavedSearch)
        ],
        "bot_links": [{"chat_id": b.chat_id, "created_at": _iso(b.created_at)} for b in rows(BotLink)],
        "bot_link_codes": [
            {"id": c.id, "code": c.code, "used_at": _iso(c.used_at), "created_at": _iso(c.created_at)}
            for c in rows(BotLinkCode)
        ],
        "audit_log": [
            {
                "id": a.id,
                "user_id": a.user_id,
                "action": a.action,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "detail": a.detail,
                # The owner's own address, in the owner's own export. It
                # is data about them, so withholding it from them while
                # keeping it in the database would be the odd choice.
                "ip_address": a.ip_address,
                "created_at": _iso(a.created_at),
            }
            for a in rows(AuditLog)
        ],
    }


def _delete_count(db: Session, statement: Delete) -> int:
    """Run a DELETE and report how many rows it removed.

    ``Session.execute`` is typed as returning a generic ``Result``, which
    does not statically carry ``rowcount``; a DELETE always yields a
    ``CursorResult`` at runtime, so the narrowing is safe here and confined
    to this one place instead of repeated at every call site.
    """
    return cast(CursorResult[Any], db.execute(statement)).rowcount


def delete_workspace(db: Session, workspace_id: int) -> dict[str, int]:
    """Erase a workspace and everything belonging to it. Irreversible.

    Returns a per-table count of what was removed, which is what makes the
    operation auditable after the fact — the audit log itself is one of the
    tables being deleted, so the count is the only record that survives.
    """
    emails = [row[0] for row in db.query(User.email).filter(User.workspace_id == workspace_id).all()]
    user_ids = [row[0] for row in db.query(User.id).filter(User.workspace_id == workspace_id).all()]

    removed: dict[str, int] = {}

    # Sessions key on user_id, not workspace_id, so they are removed by the
    # owning users rather than by the workspace.
    if user_ids:
        removed["auth_sessions"] = _delete_count(db, delete(AuthSession).where(AuthSession.user_id.in_(user_ids)))
    if emails:
        removed["login_attempts"] = _delete_count(
            db, delete(LoginAttempt).where(LoginAttempt.identifier.in_(emails))
        )

    # Rate-limit markers store the workspace id as an opaque identifier
    # string, so they are matched by value rather than by foreign key.
    removed["action_events"] = _delete_count(
        db, delete(ActionEvent).where(ActionEvent.identifier == str(workspace_id))
    )

    for model in WORKSPACE_TABLES:
        removed[model.__tablename__] = _delete_count(db, delete(model).where(model.workspace_id == workspace_id))

    removed["workspaces"] = _delete_count(db, delete(Workspace).where(Workspace.id == workspace_id))
    db.commit()
    return removed
