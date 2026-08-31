"""Channel registration. Every query is scoped to the caller's workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import account_login
from app.accounts import reactivate
from app.audit import record as audit_record
from app.database import get_db
from app.deps import get_current_user, get_session_user
from app.errors import ErrorCode, rate_limited
from app.models import Channel, TelegramAccount, User
from app.schemas import (
    AccountLoginStart,
    AccountLoginStartOut,
    AccountLoginVerify,
    AccountLoginVerifyOut,
    ChannelCreate,
    ChannelOut,
    ChannelUpdate,
    TelegramAccountOut,
)
from app.security import is_action_rate_limited, record_action_event, verify_password, waste_password_time

router = APIRouter(prefix="/channels", tags=["channels"])

# Generous enough that adding a full batch of ten accounts in one sitting
# never trips it, tight enough that a script cannot use this endpoint to
# hammer Telegram's own code-request rate limiting on the app's behalf.
ACCOUNT_LOGIN_LIMIT = 15
ACCOUNT_LOGIN_WINDOW_MINUTES = 15


@router.get("", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> list[Channel]:
    return (
        db.query(Channel)
        .filter(Channel.workspace_id == current_user.workspace_id)
        .order_by(Channel.created_at.desc())
        .all()
    )


def _owned_account_id(db: Session, account_id: int | None, user: User) -> int | None:
    """Validate that an account id belongs to the caller's workspace.

    Scoping the lookup by workspace means another workspace's account id is
    indistinguishable from one that does not exist, so ids cannot be probed
    by watching which ones are accepted.
    """
    if account_id is None:
        return None
    account = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.id == account_id, TelegramAccount.workspace_id == user.workspace_id)
        .first()
    )
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    return account.id


@router.get("/accounts", response_model=list[TelegramAccountOut])
def list_accounts(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[TelegramAccountOut]:
    """The workspace's collecting accounts, so channels can be assigned to them.

    Session strings are deliberately absent from the response model: they
    are bearer credentials for the Telegram account itself and nothing in
    the web UI ever needs to read one back.

    Health comes with the list rather than from a second endpoint: the
    question "which of my accounts is broken" is the reason to open this
    panel at all, and splitting it across two calls means the list can be
    rendered without it.
    """
    accounts = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.workspace_id == current_user.workspace_id)
        .order_by(TelegramAccount.id)
        .all()
    )

    # One grouped query rather than one per account: the panel is small
    # today, but a per-row count is the shape that quietly becomes N+1.
    #
    # Built by indexing each Row rather than handing the whole sequence to
    # ``dict()``. That shorter form needs a ``type: ignore[arg-type]``,
    # because a ``Row`` only *behaves* like a 2-tuple — and the ignore is
    # not portable: SQLAlchemy's stubs describe this well enough in newer
    # releases that mypy then flags the suppression itself as unused. With
    # the version range this project pins (``sqlalchemy>=2.0,<3.0``), CI
    # and a fresh developer install can land on either side, so *both* the
    # ignore and its absence break somebody. Indexing needs no suppression
    # under either.
    counts: dict[int | None, int] = {
        row[0]: row[1]
        for row in db.execute(
            select(Channel.account_id, func.count())
            .where(Channel.workspace_id == current_user.workspace_id)
            .group_by(Channel.account_id)
        ).all()
    }

    # Channels with no account named fall to the default (lowest-id)
    # account at collection time, so the panel attributes them the same
    # way — otherwise the default account would show zero channels while
    # actually collecting all of them.
    unassigned = counts.get(None, 0)
    default_id = accounts[0].id if accounts else None

    return [
        TelegramAccountOut(
            id=account.id,
            label=account.label,
            is_active=account.is_active,
            created_at=account.created_at,
            last_success_at=account.last_success_at,
            last_failure_at=account.last_failure_at,
            last_error=account.last_error,
            consecutive_failures=account.consecutive_failures,
            disabled_reason=account.disabled_reason,
            links_collected=account.links_collected,
            channel_count=counts.get(account.id, 0) + (unassigned if account.id == default_id else 0),
        )
        for account in accounts
    ]


@router.post("/accounts/{account_id}/reactivate", response_model=TelegramAccountOut)
def reactivate_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TelegramAccountOut:
    """Bring back an account the collector disabled automatically.

    The operator has to have fixed the underlying cause — a re-authorised
    session, a corrected encryption key — because nothing here can verify
    that. Re-enabling clears the failure streak as well as the flag: with
    the counter left where it was, the next single failure would disable
    the account again, which is not what re-enabling means.
    """
    account = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.id == account_id, TelegramAccount.workspace_id == current_user.workspace_id)
        .first()
    )
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")

    reactivate(db, account)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="account.reactivate",
        target_type="account",
        target_id=str(account.id),
    )
    db.commit()

    return TelegramAccountOut(
        id=account.id,
        label=account.label,
        is_active=account.is_active,
        created_at=account.created_at,
        last_success_at=account.last_success_at,
        last_failure_at=account.last_failure_at,
        last_error=account.last_error,
        consecutive_failures=account.consecutive_failures,
        disabled_reason=account.disabled_reason,
        links_collected=account.links_collected,
        channel_count=db.query(Channel)
        .filter(Channel.workspace_id == current_user.workspace_id, Channel.account_id == account.id)
        .count(),
    )


@router.post("/accounts/login/start", response_model=AccountLoginStartOut)
async def start_account_login(
    payload: AccountLoginStart,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> AccountLoginStartOut:
    """Step 1 of adding an account from the dashboard: send the code.

    ``get_session_user`` rather than ``get_current_user`` — an API key
    must not be able to mint a new Telegram bearer credential, only a
    browser session can. The current password is re-checked on top of
    that session, the same gate idea 55's precedent uses for
    change-password, TOTP-disable and account-delete: this action is at
    least as sensitive as any of those.
    """
    if not verify_password(payload.current_password, current_user.password_hash):
        waste_password_time()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="incorrect password")

    scope_id = str(current_user.workspace_id)
    if is_action_rate_limited(
        db, "account_login", scope_id, limit=ACCOUNT_LOGIN_LIMIT, window_minutes=ACCOUNT_LOGIN_WINDOW_MINUTES
    ):
        raise rate_limited(
            ErrorCode.RATE_LIMITED,
            "too many login attempts, please slow down",
            retry_after_seconds=ACCOUNT_LOGIN_WINDOW_MINUTES * 60,
        )
    record_action_event(db, "account_login", scope_id)

    try:
        token = await account_login.start_login(db, current_user.workspace_id, payload.label, payload.phone)
    except account_login.LoginError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="account.login_started",
        detail=payload.label,
    )
    db.commit()
    return AccountLoginStartOut(login_token=token)


@router.post("/accounts/login/verify", response_model=AccountLoginVerifyOut)
async def verify_account_login(
    payload: AccountLoginVerify,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> AccountLoginVerifyOut:
    """Step 2: the code, or — once ``needs_password`` comes back — the
    account's own two-factor password. No password re-confirmation here:
    step 1 already proved it is the operator, and this call cannot do
    anything step 1 did not already authorise."""
    try:
        account = await account_login.verify_login(
            db, current_user.workspace_id, payload.login_token, payload.code, payload.password
        )
    except account_login.NeedsPassword:
        return AccountLoginVerifyOut(status="needs_password")
    except account_login.LoginError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="account.login_completed",
        target_type="account",
        target_id=str(account.id),
        detail=account.label,
    )
    db.commit()

    return AccountLoginVerifyOut(
        status="added",
        account=TelegramAccountOut(
            id=account.id,
            label=account.label,
            is_active=account.is_active,
            created_at=account.created_at,
            last_success_at=account.last_success_at,
            last_failure_at=account.last_failure_at,
            last_error=account.last_error,
            consecutive_failures=account.consecutive_failures,
            disabled_reason=account.disabled_reason,
            links_collected=account.links_collected,
            channel_count=0,  # a freshly logged-in account has no channels assigned yet
        ),
    )


@router.post("", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
def add_channel(
    payload: ChannelCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> Channel:
    existing = (
        db.query(Channel)
        .filter(
            Channel.workspace_id == current_user.workspace_id,
            Channel.tg_channel_id == payload.tg_channel_id,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="channel already added")

    channel = Channel(
        workspace_id=current_user.workspace_id,
        tg_channel_id=payload.tg_channel_id,
        username=payload.username,
        title=payload.title,
        account_id=_owned_account_id(db, payload.account_id, current_user),
    )
    db.add(channel)
    db.flush()
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="channel.add",
        target_type="channel",
        target_id=str(channel.id),
        detail=payload.username or payload.tg_channel_id,
    )
    db.commit()
    db.refresh(channel)
    return channel


@router.patch("/{channel_id:int}", response_model=ChannelOut)
def reassign_channel(
    channel_id: int,
    payload: ChannelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Channel:
    """Move a channel to a different collecting account.

    Spreading channels across accounts is what keeps any single account
    below Telegram's per-account request rate; it is not a throughput
    feature so much as a way to avoid one account carrying every channel.
    """
    channel = (
        db.query(Channel)
        .filter(Channel.id == channel_id, Channel.workspace_id == current_user.workspace_id)
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    channel.account_id = _owned_account_id(db, payload.account_id, current_user)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="channel.reassign",
        target_type="channel",
        target_id=str(channel.id),
        detail=f"account_id -> {channel.account_id}",
    )
    db.commit()
    db.refresh(channel)
    return channel


# int converter for the same reason as GET /links/{link_id:int}: a bare
# path parameter here would also match /channels/accounts.
@router.get("/{channel_id:int}", response_model=ChannelOut)
def get_channel(
    channel_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> Channel:
    """One channel's details. A foreign id reads as 404, like a missing one."""
    channel = (
        db.query(Channel)
        .filter(Channel.id == channel_id, Channel.workspace_id == current_user.workspace_id)
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return channel


@router.delete("/{channel_id:int}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_channel(
    channel_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> Response:
    """Stop collecting from a channel without deleting the links it produced.

    Returns an explicit empty ``Response`` rather than being annotated
    ``-> None``. Under ``from __future__ import annotations`` the annotation
    ``None`` reaches FastAPI as the *class* ``NoneType``, which it reads as a
    declared response body and rejects on a 204 — so the two sibling delete
    endpoints and this one now all return ``Response`` the same way.
    """
    channel = (
        db.query(Channel)
        .filter(Channel.id == channel_id, Channel.workspace_id == current_user.workspace_id)
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    channel.is_active = False
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="channel.deactivate",
        target_type="channel",
        target_id=str(channel.id),
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
