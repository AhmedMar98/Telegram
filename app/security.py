"""Password hashing and revocable server-side session tokens.

Design choices (all resolve R-01 "no real authentication"):

- Passwords are hashed with ``bcrypt`` directly (no ``passlib`` shim, which
  has known version-compatibility breakage with modern bcrypt releases).
- A session is a random 256-bit token. The client only ever sees the raw
  token (in a signed, httponly, secure cookie); the database stores only
  its SHA-256 hash, so a leaked database dump cannot be replayed as a
  valid cookie. Sessions are looked up per-request, so revocation
  (logout, admin kill-switch) takes effect immediately instead of waiting
  for a signed cookie to expire.
"""

from __future__ import annotations

import hashlib
import secrets

import bcrypt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuthSession, User
from app.timeutil import utcnow


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> str:
    """Create a new server-side session and return the raw cookie token."""
    from datetime import timedelta

    settings = get_settings()
    token = secrets.token_urlsafe(32)
    record = AuthSession(
        user_id=user.id,
        token_hash=_hash_token(token),
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(record)
    db.commit()
    return token


def resolve_session(db: Session, token: str | None) -> User | None:
    """Return the active user for a raw cookie token, or None."""
    if not token:
        return None
    token_hash = _hash_token(token)
    record = db.query(AuthSession).filter(AuthSession.token_hash == token_hash).first()
    if record is None:
        return None
    if record.revoked_at is not None or record.expires_at < utcnow():
        return None
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(db: Session, token: str) -> None:
    token_hash = _hash_token(token)
    record = db.query(AuthSession).filter(AuthSession.token_hash == token_hash).first()
    if record is not None and record.revoked_at is None:
        record.revoked_at = utcnow()
        db.commit()
