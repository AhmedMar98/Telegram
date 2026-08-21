"""Registration, login, logout.

Registration always creates a brand-new workspace with the registering
user as its ``owner`` — this is the internal/multi-user-now,
SaaS-later shape the owner confirmed (Q-01/Q-02): every workspace is
isolated from every other from the very first row it ever writes.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.account_data import delete_workspace, export_workspace
from app.apikeys import MAX_KEYS_PER_USER, create_api_key, list_api_keys, revoke_api_key
from app.audit import record as audit_record
from app.clientinfo import client_ip, client_origin
from app.config import get_settings
from app.database import get_db
from app.deps import COOKIE_NAME, get_current_user, get_session_user
from app.errors import ErrorCode, rate_limited, unprocessable
from app.models import Channel, TelegramAccount, User, Workspace
from app.passwords import rejection_reason
from app.schemas import (
    AccountSummary,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ChangePasswordRequest,
    DeleteAccountRequest,
    DeleteAccountResponse,
    LoginRequest,
    RegisterRequest,
    SecurityActivityOut,
    SessionOut,
    WorkspaceOut,
    WorkspaceRenameRequest,
)
from app.security import (
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW_MINUTES,
    clear_login_failures,
    constant_time_equals,
    create_session,
    current_session_id,
    hash_password,
    is_locked_out,
    list_active_sessions,
    normalize_email,
    recent_failed_attempts,
    record_login_attempt,
    revoke_all_sessions,
    revoke_session,
    revoke_session_by_id,
    verify_password,
    waste_password_time,
)
from app.storage import link_count

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
def register(
    payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> dict:
    settings = get_settings()
    if settings.invite_code and not constant_time_equals(payload.invite_code, settings.invite_code):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid invite code")

    email = normalize_email(payload.email)
    existing = db.query(User).filter(User.email == email).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")

    weak = rejection_reason(payload.password)
    if weak is not None:
        raise unprocessable(ErrorCode.WEAK_PASSWORD, weak)

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

    ip, user_agent = client_origin(request)
    token = create_session(db, user, ip_address=ip, user_agent=user_agent)
    _set_session_cookie(response, token)
    return {"id": user.id, "email": user.email, "workspace_id": workspace.id}


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    email = normalize_email(payload.email)
    ip, user_agent = client_origin(request)

    if is_locked_out(db, email):
        # The lockout window is a known constant, so "later" can be an
        # actual number instead of leaving the client to guess.
        raise rate_limited(
            ErrorCode.LOGIN_THROTTLED,
            "too many failed attempts, try again later",
            retry_after_seconds=LOGIN_WINDOW_MINUTES * 60,
        )

    user = db.query(User).filter(User.email == email).first()
    if user is None or not user.is_active:
        # Burn the same time a real password check would, so a missing or
        # disabled account is indistinguishable from a wrong password.
        waste_password_time()
        record_login_attempt(db, email, successful=False, ip_address=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    if not verify_password(payload.password, user.password_hash):
        record_login_attempt(db, email, successful=False, ip_address=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    clear_login_failures(db, email)
    audit_record(db, workspace_id=user.workspace_id, user_id=user.id, action="user.login")
    db.commit()

    token = create_session(db, user, ip_address=ip, user_agent=user_agent)
    _set_session_cookie(response, token)
    return {"id": user.id, "email": user.email, "workspace_id": user.workspace_id}


@router.post("/logout")
def logout(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    if session:
        revoke_session(db, session)

    audit_record(db, workspace_id=current_user.workspace_id, user_id=current_user.id, action="user.logout")
    db.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> dict:
    workspace = db.get(Workspace, current_user.workspace_id)
    return {
        "id": current_user.id,
        "email": current_user.email,
        "workspace_id": current_user.workspace_id,
        "workspace_name": workspace.name if workspace else None,
    }


@router.get("/me/summary", response_model=AccountSummary)
def me_summary(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> AccountSummary:
    """The account at a glance, without four round trips to assemble it.

    Idea 115. Implemented as a thin aggregate over the functions that
    already answer each question, so it cannot drift away from the pages
    it summarises — see ``AccountSummary`` for why that mattered more than
    the saving in round trips.

    Scoped to the caller's own workspace throughout. There is no parameter
    naming a workspace, so this adds no new way to read across the
    isolation boundary.
    """
    workspace = db.get(Workspace, current_user.workspace_id)
    ws_id = current_user.workspace_id

    channels = (
        db.execute(select(func.count()).select_from(Channel).where(Channel.workspace_id == ws_id)).scalar() or 0
    )
    accounts = db.execute(
        select(TelegramAccount.is_active, func.count())
        .where(TelegramAccount.workspace_id == ws_id)
        .group_by(TelegramAccount.is_active)
    ).all()
    by_state = {bool(is_active): count for is_active, count in accounts}

    return AccountSummary(
        user_id=current_user.id,
        email=current_user.email,
        workspace_id=ws_id,
        workspace_name=workspace.name if workspace else None,
        member_since=current_user.created_at,
        total_links=link_count(db, ws_id),
        total_channels=channels,
        active_accounts=by_state.get(True, 0),
        disabled_accounts=by_state.get(False, 0),
        active_sessions=len(list_active_sessions(db, current_user.id)),
        failed_logins_recent=len(recent_failed_attempts(db, current_user.email)),
    )


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
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

    weak = rejection_reason(payload.new_password)
    if weak is not None:
        raise unprocessable(ErrorCode.WEAK_PASSWORD, weak)

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
    current_user: User = Depends(get_session_user),
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
    current_user: User = Depends(get_session_user),
) -> list[SessionOut]:
    """Every device/browser currently signed into this account."""
    current_id = current_session_id(db, session)
    return [
        SessionOut(
            id=s.id,
            created_at=s.created_at,
            expires_at=s.expires_at,
            is_current=s.id == current_id,
            ip_address=s.ip_address,
            user_agent=s.user_agent,
        )
        for s in list_active_sessions(db, current_user.id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session_endpoint(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
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


@router.get("/me/export")
def export_my_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """Download everything this workspace holds, as one JSON document.

    Portability matters more than usual here: the free database tier this
    runs on is time-limited, so being able to take the whole workspace out
    — not just its links — is part of the product rather than a nicety.

    This is a technical portability tool. It is deliberately *not* labelled
    a GDPR/CCPA subject-access response: the platform makes no compliance
    claim, and calling it one would be the claim.
    """
    payload = export_workspace(db, current_user.workspace_id)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="workspace.export",
        detail=f"{len(payload['links'])} link(s)",
        # Idea 81. An export is the one action that moves the whole
        # workspace out of the platform in a single request, so "it was
        # exported" without "from where" is the least useful half of the
        # record — an owner reviewing this is asking whether it was them.
        ip_address=client_ip(request),
    )
    db.commit()
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": 'attachment; filename="workspace-export.json"'},
    )


@router.post("/me/delete", response_model=DeleteAccountResponse)
def delete_my_workspace(
    payload: DeleteAccountRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> DeleteAccountResponse:
    """Erase this workspace and everything in it. There is no undo.

    Two independent confirmations are required — the account password and
    a typed literal — because the failure mode is unrecoverable and the
    request is otherwise indistinguishable from any other authenticated
    call.
    """
    if payload.confirm != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm must be the literal string DELETE",
        )
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="password is incorrect")

    removed = delete_workspace(db, current_user.workspace_id)
    return DeleteAccountResponse(deleted=removed)


@router.patch("/workspace", response_model=WorkspaceOut)
def rename_workspace(
    payload: WorkspaceRenameRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Rename the caller's own workspace.

    The workspace is resolved from the session rather than from a path or
    body parameter, so there is no id a caller could substitute to rename
    somebody else's.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name cannot be blank",
        )

    workspace = db.get(Workspace, current_user.workspace_id)
    if workspace is None:  # pragma: no cover - a live session implies a workspace
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")

    previous = workspace.name
    workspace.name = name
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="workspace.rename",
        target_type="workspace",
        target_id=str(workspace.id),
        detail=f"{previous} -> {name}",
    )
    db.commit()
    db.refresh(workspace)
    return workspace


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_my_api_keys(
    db: Session = Depends(get_db), current_user: User = Depends(get_session_user)
) -> list[ApiKeyOut]:
    """Keys this account still holds. Never includes any key's raw value.

    Session-only, like every endpoint on this resource — see
    ``app/apikeys.py`` for why a key must not be able to enumerate or
    issue keys.
    """
    return [ApiKeyOut.model_validate(k) for k in list_api_keys(db, current_user)]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_my_api_key(
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> ApiKeyCreated:
    """Issue a key and return it once.

    Capped per user. Not an anti-abuse measure — the cap is on your own
    account — but a key you have forgotten about is a credential nobody is
    watching, and an unbounded list makes that the normal state.
    """
    if len(list_api_keys(db, current_user)) >= MAX_KEYS_PER_USER:
        raise unprocessable(
            ErrorCode.API_KEY_LIMIT,
            f"at most {MAX_KEYS_PER_USER} active API keys; revoke one first",
        )

    record, raw = create_api_key(db, current_user, name=payload.name)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="apikey.create",
        target_type="api_key",
        target_id=str(record.id),
        detail=record.prefix,
    )
    db.commit()
    # The raw value exists in this response and nowhere else, now or ever.
    return ApiKeyCreated(key=raw, api_key=ApiKeyOut.model_validate(record))


@router.delete("/api-keys/{key_id:int}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> Response:
    """Revoke one key immediately. Another user's id reads as 404."""
    if not revoke_api_key(db, current_user, key_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="api key not found")

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="apikey.revoke",
        target_type="api_key",
        target_id=str(key_id),
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/security-activity", response_model=SecurityActivityOut)
def security_activity(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> SecurityActivityOut:
    """Failed sign-in attempts on this account, so the owner can notice an attack.

    The throttle in ``app/security.py`` already blocks online guessing;
    this makes it *visible*, which is a different job — a lockout the
    account owner never sees tells them nothing about whether someone is
    trying.

    Scoped to the caller's own email. There is no parameter to inspect a
    different account, so this cannot be turned into a way to probe
    whether some other address is under attack.
    """
    attempts = recent_failed_attempts(db, current_user.email)
    distinct_ips = sorted({a.ip_address for a in attempts if a.ip_address})
    return SecurityActivityOut(
        failed_attempts=len(attempts),
        window_minutes=LOGIN_WINDOW_MINUTES,
        lockout_threshold=LOGIN_MAX_FAILURES,
        distinct_ip_count=len(distinct_ips),
        last_failed_at=attempts[0].created_at if attempts else None,
    )
