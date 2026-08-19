"""Channel registration. Every query is scoped to the caller's workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.audit import record as audit_record
from app.database import get_db
from app.deps import get_current_user
from app.models import Channel, User
from app.schemas import ChannelCreate, ChannelOut

router = APIRouter(prefix="/channels", tags=["channels"])


@router.get("", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> list[Channel]:
    return (
        db.query(Channel)
        .filter(Channel.workspace_id == current_user.workspace_id)
        .order_by(Channel.created_at.desc())
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
