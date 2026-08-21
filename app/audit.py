"""Tiny helper to keep audit-log writes one-line and consistent."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog

# The actions a security review cares about, as opposed to routine
# collection activity. Listed explicitly rather than pattern-matched on
# the action name: a new sensitive action must be added here on purpose,
# and a rename must not silently drop it out of the security export.
AUDITED_SECURITY_ACTIONS: tuple[str, ...] = (
    "user.register",
    "user.login",
    "user.logout",
    "user.change_password",
    "user.logout_all",
    "user.revoke_session",
    "apikey.create",
    "apikey.revoke",
    "totp.setup_started",
    "totp.enabled",
    "totp.disabled",
    "totp.recovery_regenerated",
    "workspace.export",
    "workspace.security_export",
    "workspace.rename",
)


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
