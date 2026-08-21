"""The notification centre, its history, and the switches that govern it.

Ideas 156, 157, 161 and 165. All four read or write the same two tables —
the centre, the audit trail and the recent-activity strip are one set of
rows viewed three ways, and the preferences are what decide whether a row
is ever created.

Session-only throughout, like every other settings surface: an API key
that could switch a workspace's alerts off would be a way to silence the
very warnings that report a compromise.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts import ALERT_TYPES, default_for
from app.audit import record as audit_record
from app.database import get_db
from app.deps import get_session_user
from app.errors import ErrorCode, unprocessable
from app.models import Notification, NotificationPreference, User
from app.notify import set_preference
from app.schemas import (
    AlertPreferenceOut,
    AlertPreferenceUpdate,
    NotificationListResponse,
    NotificationOut,
)
from app.timeutil import utcnow

router = APIRouter(prefix="/notifications", tags=["notifications"])

# The strip at the top of the dashboard shows the most recent handful
# (idea 165); the centre pages through the rest (156).
RECENT_LIMIT = 10


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    limit: int = Query(default=RECENT_LIMIT, ge=1, le=100),
    unread_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> NotificationListResponse:
    """Everything the platform has raised for this workspace, newest first."""
    base = db.query(Notification).filter(Notification.workspace_id == current_user.workspace_id)

    total = base.count()
    unread = base.filter(Notification.read_at.is_(None)).count()

    query = base.filter(Notification.read_at.is_(None)) if unread_only else base
    rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit).all()

    return NotificationListResponse(
        total=total, unread=unread, items=[NotificationOut.model_validate(r) for r in rows]
    )


@router.post("/{notification_id:int}/read", response_model=NotificationOut)
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> Notification:
    """Mark one as read. Another workspace's id reads as 404, like any other."""
    row = (
        db.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.workspace_id == current_user.workspace_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="notification not found")

    if row.read_at is None:
        row.read_at = utcnow()
        db.commit()
    return row


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_read(db: Session = Depends(get_db), current_user: User = Depends(get_session_user)) -> Response:
    """Clear the unread badge without deleting the history.

    Read and deleted are kept apart deliberately: the audit value of idea
    161 is that the record survives being acknowledged.
    """
    db.query(Notification).filter(
        Notification.workspace_id == current_user.workspace_id, Notification.read_at.is_(None)
    ).update({Notification.read_at: utcnow()}, synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/preferences", response_model=list[AlertPreferenceOut])
def list_preferences(
    db: Session = Depends(get_db), current_user: User = Depends(get_session_user)
) -> list[AlertPreferenceOut]:
    """Every alert type this platform can send, and whether it is on.

    The full catalogue is returned rather than only the stored rows: a
    switch you cannot see is a switch you cannot turn off, which is the
    failure the exit criterion is guarding against.
    """
    stored = {
        row.alert_type: row.enabled
        for row in db.execute(
            select(NotificationPreference).where(NotificationPreference.workspace_id == current_user.workspace_id)
        ).scalars()
    }

    return [
        AlertPreferenceOut(
            key=alert.key,
            label=alert.label,
            description=alert.description,
            enabled=stored.get(alert.key, alert.default_on),
            is_default=alert.key not in stored,
        )
        for alert in ALERT_TYPES
    ]


@router.patch("/preferences/{alert_type}", response_model=AlertPreferenceOut)
def update_preference(
    alert_type: str,
    payload: AlertPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> AlertPreferenceOut:
    """Switch one alert type on or off for this workspace."""
    if not set_preference(db, current_user.workspace_id, alert_type, payload.enabled):
        raise unprocessable(
            ErrorCode.UNKNOWN_ALERT_TYPE,
            f"unknown alert type: {alert_type}",
        )

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="notification.preference",
        target_type="alert_type",
        target_id=alert_type,
        detail="enabled" if payload.enabled else "disabled",
    )
    db.commit()

    alert = next(a for a in ALERT_TYPES if a.key == alert_type)
    return AlertPreferenceOut(
        key=alert.key,
        label=alert.label,
        description=alert.description,
        enabled=payload.enabled,
        is_default=False,
    )


@router.get("/unread-count")
def unread_count(db: Session = Depends(get_db), current_user: User = Depends(get_session_user)) -> dict:
    """Just the badge number, for a poll that should stay cheap."""
    count = (
        db.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.workspace_id == current_user.workspace_id,
                Notification.read_at.is_(None),
            )
        ).scalar()
        or 0
    )
    return {"unread": count}


# Referenced so the defaults stay importable from one place if a caller
# needs to explain why a switch reads the way it does.
__all__ = ["router", "default_for"]
