"""Personal API keys: the cookie's replacement for programmatic callers.

Idea 80, and the declared prerequisite for every external-integration idea
in this phase — an integration with no credential of its own would have to
be handed a session cookie, which is the thing this exists to avoid.

**The rule that shapes everything here: a key is not a login.**

A stolen cookie already lets someone browse a workspace. A key is worse in
one specific way — it is designed to be pasted into scripts, CI files and
third-party automation, so it leaks through channels a cookie never
touches. That makes the interesting question not "what can a key do" but
*what must it never do*:

- **It cannot destroy the account.** ``POST /auth/me/delete`` and
  ``POST /auth/change-password`` take the session only. A key found in a
  committed ``.env`` must not be enough to erase the collection or lock
  the owner out of it.
- **It cannot mint or list keys.** Otherwise revocation is theatre: anyone
  holding a leaked key issues a second one before the first is revoked,
  and the owner revokes a credential the attacker stopped using. Key
  management is session-only, always.

Both restrictions are enforced by a *separate dependency*
(``get_session_user``) rather than by a check inside each endpoint,
because a check that has to be remembered is a check that eventually is
not.

Storage matches ``app/security.py``: SHA-256, not bcrypt. These tokens are
256 bits of ``secrets`` output, so there is no low-entropy guess to slow
down — bcrypt would buy nothing and cost a KDF on every request.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy.orm import Session

from app.models import ApiKey, User
from app.timeutil import utcnow

# A recognisable scheme, so a leaked key is identifiable as one. Secret
# scanners match on fixed prefixes, and a key that looks like generic
# base64 is a key nobody notices in a diff.
KEY_SCHEME = "lipk_"
PREFIX_LENGTH = 12
MAX_KEYS_PER_USER = 10


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def looks_like_key(value: str | None) -> bool:
    """Whether a bearer value is even shaped like one of our keys.

    Lets the caller skip a database round trip for an ``Authorization``
    header meant for something else entirely.
    """
    return bool(value) and value.startswith(KEY_SCHEME)  # type: ignore[union-attr]


def create_api_key(db: Session, user: User, *, name: str) -> tuple[ApiKey, str]:
    """Issue a key. Returns the record and the raw value, shown once.

    The caller is responsible for the "once": nothing here stores the raw
    key, and no later read can recover it.
    """
    raw = KEY_SCHEME + secrets.token_urlsafe(32)
    record = ApiKey(
        workspace_id=user.workspace_id,
        user_id=user.id,
        name=name,
        token_hash=_hash_key(raw),
        prefix=raw[:PREFIX_LENGTH],
    )
    db.add(record)
    db.flush()
    return record, raw


def resolve_api_key(db: Session, raw: str | None) -> User | None:
    """Return the user for a raw key, or None. Never raises.

    Records use as a side effect. That write is the whole reason the
    dashboard can answer "is this key still in use, and can I revoke it?"
    — a key nobody can account for is one nobody dares revoke.

    Use is counted even when the endpoint goes on to refuse the key
    (``get_session_user`` resolves it only to answer 403 rather than 401).
    That is deliberate: a key being presented over and over to endpoints
    that reject it is precisely the shape of a leaked credential being
    probed, and it is the counter that would show it.
    """
    if not looks_like_key(raw):
        return None

    record = db.query(ApiKey).filter(ApiKey.token_hash == _hash_key(raw or "")).first()
    if record is None or record.revoked_at is not None:
        return None

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        return None

    record.last_used_at = utcnow()
    record.use_count = (record.use_count or 0) + 1
    db.commit()
    return user


def active_key_for(db: Session, raw: str | None) -> ApiKey | None:
    """The key record behind a raw value, without recording a use.

    Separate from ``resolve_api_key`` so rate limiting and auditing can
    name the key without inflating its own use counter.
    """
    if not looks_like_key(raw):
        return None
    record = db.query(ApiKey).filter(ApiKey.token_hash == _hash_key(raw or "")).first()
    return record if record is not None and record.revoked_at is None else None


def list_api_keys(db: Session, user: User) -> list[ApiKey]:
    """Every key this user still holds, newest first. Revoked ones are gone."""
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
        .all()
    )


def revoke_api_key(db: Session, user: User, key_id: int) -> bool:
    """Revoke one key. Returns False if it is not this user's, or already gone.

    Scoped by ``user_id`` in the query rather than checked afterwards, so
    there is no ordering in which a foreign id gets revoked.
    """
    record = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .first()
    )
    if record is None:
        return False
    record.revoked_at = utcnow()
    return True
