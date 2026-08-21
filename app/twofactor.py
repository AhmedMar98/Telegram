"""Applying the second factor to a user row: verify, enable, disable.

``app/totp.py`` holds the cryptography and knows nothing about the
database. This module is the seam between them, so the persistence rules
that make the feature safe live in one place rather than being repeated at
each call site:

- A code is accepted **once**. The step it belongs to is written back
  immediately, so the same code presented again inside its own window is
  refused.
- A recovery code is **removed** when spent, in the same transaction that
  accepts it.
- Enabling requires a working code first. The secret is stored before the
  switch is flipped, and the switch only flips once the authenticator has
  proved itself — a mistyped setup therefore leaves the account exactly as
  it was rather than locked out of itself.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import User
from app.totp import (
    consume_recovery_code,
    decrypt_secret,
    encrypt_secret,
    new_recovery_codes,
    new_secret,
    verify_code,
)


def _recovery_hashes(user: User) -> list[str]:
    return [line for line in (user.totp_recovery_hashes or "").splitlines() if line]


def remaining_recovery_codes(user: User) -> int:
    return len(_recovery_hashes(user))


def accept_second_factor(db: Session, user: User, code: str | None) -> bool:
    """Verify a TOTP code or a recovery code, spending it either way.

    Both kinds are accepted here rather than at separate endpoints,
    because the person using one is locked out and reaching for whatever
    they have — asking them to first classify which sort of code they are
    holding is friction at precisely the wrong moment.
    """
    if not code or not user.totp_enabled or not user.totp_secret:
        return False

    secret = decrypt_secret(user.totp_secret)
    if secret is not None:
        step = verify_code(secret, code, last_step=user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            db.commit()
            return True
    # A secret that will not decrypt (a rotated-away encryption key) is not
    # a wrong code. Recovery codes still work, which is the point of having
    # them: the account is reachable even when the secret is not.

    remaining = consume_recovery_code(code, _recovery_hashes(user))
    if remaining is not None:
        user.totp_recovery_hashes = "\n".join(remaining)
        db.commit()
        return True

    return False


def begin_enrolment(db: Session, user: User) -> tuple[str, str]:
    """Start setup: store a fresh secret, return it and its otpauth URI.

    Deliberately does **not** enable anything. Until a code proves the
    authenticator holds the same secret, the account logs in exactly as
    before.

    Starting again replaces any half-finished secret, so an abandoned
    setup cannot leave a stale one lying around to be enabled later.
    """
    secret = new_secret()
    user.totp_secret = encrypt_secret(secret)
    user.totp_enabled = False
    user.totp_last_step = None
    db.commit()
    from app.totp import provisioning_uri

    return secret, provisioning_uri(secret, email=user.email)


def complete_enrolment(db: Session, user: User, code: str) -> list[str] | None:
    """Finish setup by proving the authenticator works. Returns recovery codes.

    The codes are returned in plaintext exactly once — only their hashes
    are stored — and issuing them is part of *enabling*, not a later
    optional step. On this platform (one user per workspace, no admin) a
    second factor without a recovery path is a way to lose the collection.
    """
    secret = decrypt_secret(user.totp_secret or "")
    if secret is None:
        return None

    step = verify_code(secret, code, last_step=user.totp_last_step)
    if step is None:
        return None

    plaintext, hashes = new_recovery_codes()
    user.totp_enabled = True
    user.totp_last_step = step
    user.totp_recovery_hashes = "\n".join(hashes)
    db.commit()
    return plaintext


def regenerate_recovery_codes(db: Session, user: User) -> list[str]:
    """Issue a fresh set, invalidating every previous code.

    Replacing rather than appending: codes are printed and filed, and a
    set that grows means an old printout keeps working forever.
    """
    plaintext, hashes = new_recovery_codes()
    user.totp_recovery_hashes = "\n".join(hashes)
    db.commit()
    return plaintext


def disable(db: Session, user: User) -> None:
    """Turn the second factor off and erase everything belonging to it.

    The secret and the recovery hashes go too. Keeping them would leave a
    credential in the database for a feature the account no longer uses,
    and re-enabling later should start from a new secret anyway.
    """
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_step = None
    user.totp_recovery_hashes = None
    db.commit()
