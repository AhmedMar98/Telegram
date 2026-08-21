"""Optional second factor, and the recovery path that makes it survivable.

Idea 79. ``pyotp`` is free, pure Python, 45 KB and has no runtime
dependencies, so the second factor costs no service, no subscription and
no background process — the constraints this project runs under.

**Recovery codes are not a nicety here; they are a condition of the
feature being correct.** This platform is one user per workspace with no
administrator (phase 6b is deliberately deferred), so a lost authenticator
is not "contact support" — it is a permanent lockout with the entire
collection behind it. Enabling TOTP without issuing recovery codes would
be shipping a way to destroy access to your own data.

Three further decisions, each for a specific failure:

- **The secret is encrypted at rest**, like a Telethon session string and
  for the same reason: it must be recoverable to verify a code, so hashing
  is not available, and a database dump holding plaintext TOTP secrets is
  a second-factor bypass rather than a data leak.
- **Recovery codes are hashed**, because unlike the secret they never need
  to be read back — only compared. They are single-use bearer credentials,
  and storing them in the clear would make the dump worse than no second
  factor at all.
- **A used code cannot be replayed.** A TOTP code stays valid for its
  whole time step, so without this someone who observes one has the rest
  of that window to reuse it. The last accepted step is recorded and
  anything at or before it is refused.
"""

from __future__ import annotations

import hashlib
import secrets

import pyotp

from app.crypto import InvalidToken, decrypt_field, encrypt_field

# One step either side of now, i.e. a ±30 second tolerance. Enough for a
# phone clock that drifts; small enough that an observed code is not
# useful for long. Wider windows are the usual quiet weakening of TOTP.
VALID_WINDOW = 1
TIME_STEP = 30

RECOVERY_CODE_COUNT = 10
# Four bytes of entropy per half, rendered as two readable groups. Short
# enough to type off paper, long enough that guessing one is hopeless
# against the login throttle that already exists.
_RECOVERY_BYTES = 4

ISSUER = "Link Intelligence"


def new_secret() -> str:
    """A fresh base32 TOTP secret, ready to encrypt and store."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str) -> str:
    """The ``otpauth://`` URI an authenticator app scans or accepts as text.

    Returned to the user once, during setup, and never stored — it embeds
    the secret, so treating it as displayable-again would undo the point of
    encrypting the column.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def current_step(*, at: int | None = None) -> int:
    """The 30-second time step a code belongs to, used for replay defence."""
    import time

    return int((at if at is not None else time.time()) // TIME_STEP)


def verify_code(secret: str, code: str, *, last_step: int | None = None) -> int | None:
    """Check a code and return the step it belongs to, or None.

    Returning the step rather than a boolean is what lets the caller store
    it and refuse a replay. A code from a step at or before ``last_step``
    is rejected even though it is arithmetically valid.
    """
    cleaned = (code or "").strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit():
        return None

    totp = pyotp.TOTP(secret)
    now = current_step()
    for offset in range(-VALID_WINDOW, VALID_WINDOW + 1):
        step = now + offset
        if last_step is not None and step <= last_step:
            continue
        if secrets.compare_digest(totp.at(step * TIME_STEP), cleaned):
            return step
    return None


def encrypt_secret(secret: str) -> str:
    return encrypt_field(secret)


def decrypt_secret(stored: str) -> str | None:
    """The stored secret, or None if it cannot be read.

    A secret encrypted under a rotated-away key is unreadable, and the
    honest answer is "this cannot be verified" — never "the code is
    wrong", which would send someone to re-check an authenticator that is
    working perfectly.
    """
    try:
        return decrypt_field(stored)
    except InvalidToken:
        return None


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(_normalise_recovery(code).encode("utf-8")).hexdigest()


def _normalise_recovery(code: str) -> str:
    """Compare recovery codes the way people actually type them.

    Off paper, in a hurry, locked out: case and separators are noise. The
    entropy is in the characters, not their presentation.
    """
    return (code or "").strip().lower().replace("-", "").replace(" ", "")


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """Fresh recovery codes: the plaintext to show once, and hashes to store."""
    codes = [
        f"{secrets.token_hex(_RECOVERY_BYTES)}-{secrets.token_hex(_RECOVERY_BYTES)}"
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    return codes, [_hash_recovery(code) for code in codes]


def consume_recovery_code(code: str, stored_hashes: list[str]) -> list[str] | None:
    """Spend one recovery code. Returns the remaining hashes, or None.

    Single use by construction: the match is removed from the list the
    caller then persists. A code that could be used twice is a password
    with extra steps.
    """
    candidate = _hash_recovery(code)
    for index, stored in enumerate(stored_hashes):
        if secrets.compare_digest(stored, candidate):
            return stored_hashes[:index] + stored_hashes[index + 1 :]
    return None
