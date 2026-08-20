"""Channel registration. Every query is scoped to the caller's workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

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
) -> list[TelegramAccount]:
    """The workspace's collecting accounts, so channels can be assigned to them.

    Session strings are deliberately absent from the response model: they
    are bearer credentials for the Telegram account itself and nothing in
    the web UI ever needs to read one back.
    """
    return (
        db.query(TelegramAccount)
        .filter(TelegramAccount.workspace_id == current_user.workspace_id)
        .order_by(TelegramAccount.id)
        .all()
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


@router.patch("/{channel_id}", response_model=ChannelOut)
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


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_channel(
    channel_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> None:
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
