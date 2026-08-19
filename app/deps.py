"""Shared FastAPI dependencies: DB session and the logged-in user."""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import resolve_session

COOKIE_NAME = "session"


def get_current_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    user = resolve_session(db, session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return user


def get_optional_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User | None:
    return resolve_session(db, session)
