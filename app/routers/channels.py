"""Channel registration. Every query is scoped to the caller's workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import reactivate
from app.audit import record as audit_record
from app.database import get_db
from app.deps import get_current_user
from app.models import Channel, TelegramAccount, User
from app.schemas import ChannelCreate, ChannelOut, ChannelUpdate, TelegramAccountOut

router = APIRouter(prefix="/channels", tags=["channels"])


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
