"""Registration, login, logout.

Registration always creates a brand-new workspace with the registering
user as its ``owner`` — this is the internal/multi-user-now,
SaaS-later shape the owner confirmed (Q-01/Q-02): every workspace is
isolated from every other from the very first row it ever writes.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.audit import record as audit_record
from app.config import get_settings
from app.database import get_db
from app.deps import COOKIE_NAME, get_current_user
from app.models import User, Workspace
from app.schemas import ChangePasswordRequest, LoginRequest, RegisterRequest, SessionOut
from app.security import (
    clear_login_failures,
    constant_time_equals,
    create_session,
    current_session_id,
    hash_password,
    is_locked_out,
    list_active_sessions,
    normalize_email,
    record_login_attempt,
    revoke_all_sessions,
    revoke_session,
    revoke_session_by_id,
    verify_password,
    waste_password_time,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_MAX_AGE = 60 * 60 * 24 * 14  # 14 days, matches Settings.session_ttl_hours default


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if settings.invite_code and not constant_time_equals(payload.invite_code, settings.invite_code):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid invite code")

    email = normalize_email(payload.email)
    existing = db.query(User).filter(User.email == email).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")

    workspace = Workspace(name=payload.workspace_name)
    db.add(workspace)
    db.flush()

    user = User(
        workspace_id=workspace.id,
        email=email,
        password_hash=hash_password(payload.password),
        role="owner",
    )
    db.add(user)
    db.flush()

    audit_record(
        db,
        workspace_id=workspace.id,
        user_id=user.id,
        action="user.register",
        target_type="user",
        target_id=str(user.id),
    )
    db.commit()

    token = create_session(db, user)
    _set_session_cookie(response, token)
    return {"id": user.id, "email": user.email, "workspace_id": workspace.id}


@router.post("/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    email = normalize_email(payload.email)

    if is_locked_out(db, email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts, try again later",
        )

    user = db.query(User).filter(User.email == email).first()
    if user is None or not user.is_active:
        # Burn the same time a real password check would, so a missing or
        # disabled account is indistinguishable from a wrong password.
        waste_password_time()
        record_login_attempt(db, email, successful=False)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    if not verify_password(payload.password, user.password_hash):
        record_login_attempt(db, email, successful=False)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    clear_login_failures(db, email)
    audit_record(db, workspace_id=user.workspace_id, user_id=user.id, action="user.login")
    db.commit()

    token = create_session(db, user)
    _set_session_cookie(response, token)
    return {"id": user.id, "email": user.email, "workspace_id": user.workspace_id}


@router.post("/logout")
def logout(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    if session:
        revoke_session(db, session)

    audit_record(db, workspace_id=current_user.workspace_id, user_id=current_user.id, action="user.logout")
    db.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)) -> dict:
    return {"id": current_user.id, "email": current_user.email, "workspace_id": current_user.workspace_id}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Change the account password and revoke every other active session.

    Everywhere else the account was logged in is signed out immediately —
    the whole point of a password change is to cut off access from a
    session that might be compromised. The session used to make this
    request stays alive so the person is not logged out of their own
    change.
    """
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="current password is incorrect")

    current_user.password_hash = hash_password(payload.new_password)
    revoked = revoke_all_sessions(db, current_user.id, except_token=session)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="user.change_password",
        detail=f"{revoked} other session(s) revoked",
    )
    db.commit()

    if session:
        _set_session_cookie(response, session)
    return {"ok": True, "other_sessions_revoked": revoked}


@router.post("/logout-all")
def logout_all(
    response: Response,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Sign out of every device, including this one."""
    revoked = revoke_all_sessions(db, current_user.id)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="user.logout_all",
        detail=f"{revoked} session(s) revoked",
    )
    db.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True, "sessions_revoked": revoked}


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[SessionOut]:
    """Every device/browser currently signed into this account."""
    current_id = current_session_id(db, session)
    return [
        SessionOut(id=s.id, created_at=s.created_at, expires_at=s.expires_at, is_current=s.id == current_id)
        for s in list_active_sessions(db, current_user.id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session_endpoint(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Sign out one specific device without touching any other session."""
    found = revoke_session_by_id(db, current_user.id, session_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="user.revoke_session",
        target_type="session",
        target_id=str(session_id),
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
