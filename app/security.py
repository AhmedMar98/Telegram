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
from datetime import timedelta
from functools import lru_cache

import bcrypt
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ActionEvent, AuthSession, LoginAttempt, User
from app.timeutil import utcnow

# Brute-force throttle. Counted per identifier (the submitted email) over a
# rolling window; generous enough that a person fat-fingering their password
# is never affected, tight enough that online guessing is not viable.
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_FAILURES = 8


def hash_password(raw: str) -> str:
    rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    """A real bcrypt hash used to burn time when an account does not exist."""
    return hash_password("decoy-password-for-constant-time-login")


def waste_password_time() -> None:
    """Spend the same work as a real check, so a miss is not faster than a hit.

    Without this, "no such user" returns before any hashing happens and
    "wrong password" returns after ~100ms of bcrypt, which is a reliable
    oracle for enumerating which addresses have accounts.
    """
    verify_password("decoy-password-for-constant-time-login", _decoy_hash())


def constant_time_equals(left: str | None, right: str | None) -> bool:
    """Compare two secrets without leaking their common prefix length."""
    if left is None or right is None:
        return False
    return secrets.compare_digest(left, right)


def normalize_email(email: str) -> str:
    """Fold addresses to one canonical form so casing cannot fork an account."""
    return email.strip().lower()


def _window_start():
    return utcnow() - timedelta(minutes=LOGIN_WINDOW_MINUTES)


def record_login_attempt(db: Session, identifier: str, *, successful: bool) -> None:
    """Log an attempt and opportunistically prune ones outside the window."""
    db.add(LoginAttempt(identifier=identifier, successful=successful))
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < _window_start()))
    db.commit()


def recent_failure_count(db: Session, identifier: str) -> int:
    return (
        db.query(LoginAttempt)
        .filter(
            LoginAttempt.identifier == identifier,
            LoginAttempt.successful.is_(False),
            LoginAttempt.created_at >= _window_start(),
        )
        .count()
    )


def is_locked_out(db: Session, identifier: str) -> bool:
    return recent_failure_count(db, identifier) >= LOGIN_MAX_FAILURES


def clear_login_failures(db: Session, identifier: str) -> None:
    db.execute(
        delete(LoginAttempt).where(LoginAttempt.identifier == identifier, LoginAttempt.successful.is_(False))
    )
    db.commit()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> str:
    """Create a new server-side session and return the raw cookie token."""
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


def current_session_id(db: Session, token: str | None) -> int | None:
    """The id of the AuthSession backing this cookie, for marking it as 'current' in a list."""
    if not token:
        return None
    record = db.query(AuthSession).filter(AuthSession.token_hash == _hash_token(token)).first()
    return record.id if record else None


def list_active_sessions(db: Session, user_id: int) -> list[AuthSession]:
    """Every currently-usable session for a user, newest first."""
    return (
        db.query(AuthSession)
        .filter(
            AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None), AuthSession.expires_at >= utcnow()
        )
        .order_by(AuthSession.created_at.desc())
        .all()
    )


def revoke_session_by_id(db: Session, user_id: int, session_id: int) -> bool:
    """Revoke one session by id, scoped to its owner. Returns whether it existed.

    Scoping by ``user_id`` in the query itself — rather than fetching then
    checking — means another user's session id is indistinguishable from
    one that does not exist, so ids cannot be probed.
    """
    record = db.query(AuthSession).filter(AuthSession.id == session_id, AuthSession.user_id == user_id).first()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = utcnow()
    db.commit()
    return True


def revoke_all_sessions(db: Session, user_id: int, *, except_token: str | None = None) -> int:
    """Revoke every active session for a user, optionally sparing one.

    Sparing the caller's own current session (``except_token``) is what
    lets a password change take effect everywhere else without logging
    the person out of the tab they just used to change it.

    Returns the number of sessions revoked.
    """
    except_hash = _hash_token(except_token) if except_token else None
    query = db.query(AuthSession).filter(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_hash is not None:
        query = query.filter(AuthSession.token_hash != except_hash)
    count = query.update({"revoked_at": utcnow()})
    db.commit()
    return count


def record_action_event(db: Session, scope: str, identifier: str) -> None:
    """Mark that a rate-limited action happened, for later counting."""
    db.add(ActionEvent(scope=scope, identifier=identifier))
    db.commit()


def is_action_rate_limited(db: Session, scope: str, identifier: str, *, limit: int, window_minutes: int) -> bool:
    """Whether ``identifier`` has hit ``limit`` occurrences of ``scope`` recently.

    Also opportunistically prunes events for this scope+identifier older
    than the window, so the table does not grow without bound under
    steady use.
    """
    window_start = utcnow() - timedelta(minutes=window_minutes)
    db.execute(
        delete(ActionEvent).where(
            ActionEvent.scope == scope, ActionEvent.identifier == identifier, ActionEvent.created_at < window_start
        )
    )
    db.commit()
    count = (
        db.query(ActionEvent)
        .filter(
            ActionEvent.scope == scope,
            ActionEvent.identifier == identifier,
            ActionEvent.created_at >= window_start,
        )
        .count()
    )
    return count >= limit
