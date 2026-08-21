"""Shared FastAPI dependencies: DB session and the calling user.

Two ways in, and the difference between them is a security boundary, not a
convenience:

- ``get_current_user`` accepts a session cookie **or** a personal API key.
  Reading and adding links works from a script.
- ``get_session_user`` accepts the cookie only. Anything that destroys the
  account or manages credentials uses this one, so a leaked key cannot
  erase a workspace or quietly issue itself a successor.

Splitting it into two dependencies rather than checking inside each
endpoint is deliberate: a rule enforced by which dependency an endpoint
declares cannot be forgotten halfway down a function, and shows up in the
signature where a reviewer sees it.
"""

from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.apikeys import resolve_api_key
from app.database import get_db
from app.models import User
from app.security import resolve_session

COOKIE_NAME = "session"

_NOT_AUTHENTICATED = "not authenticated"
# Said plainly rather than as a generic 401: the caller did authenticate,
# with a credential this endpoint deliberately does not accept, and
# "not authenticated" would send them to re-check a key that is fine.
_SESSION_REQUIRED = "this endpoint requires a signed-in session, not an API key"


def _bearer(authorization: str | None) -> str | None:
    """The token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def get_current_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """The caller, by cookie or API key.

    The cookie is tried first so a browser session is never charged a use
    against a key that happens to be present in the same request.
    """
    user = resolve_session(db, session) or resolve_api_key(db, _bearer(authorization))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)
    return user


def get_session_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """The caller, by session cookie only — for irreversible or credential work.

    A valid API key is refused here with 403 rather than 401. The
    distinction matters to whoever is debugging: 401 means "your
    credential is not valid", and sending that for a perfectly good key
    would point them at the wrong problem entirely.
    """
    user = resolve_session(db, session)
    if user is not None:
        return user

    if resolve_api_key(db, _bearer(authorization)) is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_SESSION_REQUIRED)

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED)


def get_optional_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User | None:
    return resolve_session(db, session)
