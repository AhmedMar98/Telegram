"""Tiny helper to keep audit-log writes one-line and consistent."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    db: Session,
    *,
    workspace_id: int,
    user_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
    ip_address: str | None = None,
) -> None:
    db.add(
        AuditLog(
            workspace_id=workspace_id,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip_address=ip_address,
        )
    )
