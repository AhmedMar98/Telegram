"""Password policy, reviewed against NIST SP 800-63B §5.1.1.2.

Where the existing policy already complied, nothing changed — the point of
the review was to find the gap, not to add ceremony:

- **8-character minimum** — meets the guideline's floor.
- **200-character maximum** — the guideline asks verifiers to accept at
  least 64; a cap exists only so a single request cannot carry an
  unbounded string into bcrypt.
- **No composition rules** — no forced mixed case, digits or symbols. The
  guideline explicitly advises against them: they push people toward
  predictable substitutions ("Password1!") without adding real entropy.
- **No forced expiry** — also advised against; rotation on a schedule
  produces incrementing suffixes, not better secrets.

The gap it found: §5.1.1.2 says verifiers *shall* compare a prospective
password against a list of commonly-used, expected or compromised values.
We had no such check, so a password of "password" was accepted.

## What this check is, and is not

It is a small embedded list of the passwords that actually appear at the
top of credential-stuffing and breach corpora, plus a few patterns
specific to this project (its own name, "telegram"). Those are what an
attacker tries first, so they are what a blocklist buys the most by
refusing.

It is **not** a full breach-corpus check. Doing that properly means the
Have I Been Pwned range API — a network call on every registration,
against a third-party service, on a request path that must keep working
when that service is down. That trade is not worth it here, and pretending
a 100-entry list equals 800 million records would be exactly the kind of
inflated claim this project rejects elsewhere.

Comparison is case-insensitive and ignores surrounding whitespace, because
"Password" and "password " are the same guess to an attacker.
"""

from __future__ import annotations

# Ordered roughly by real-world frequency in published breach analyses.
# Kept deliberately short: the long tail adds little, and every entry here
# is a password whose appearance in a leak list is not in dispute.
_COMMON_PASSWORDS = frozenset(
    {
        "123456",
        "123456789",
        "12345678",
        "1234567890",
        "12345",
        "1234567",
        "password",
        "password1",
        "password123",
        "passw0rd",
        "qwerty",
        "qwerty123",
        "qwertyuiop",
        "abc123",
        "abcd1234",
        "111111",
        "1111111",
        "123123",
        "000000",
        "iloveyou",
        "admin",
        "admin123",
        "administrator",
        "welcome",
        "welcome1",
        "welcome123",
        "monkey",
        "dragon",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "letmein",
        "letmein123",
        "trustno1",
        "master",
        "shadow",
        "superman",
        "michael",
        "jennifer",
        "jordan23",
        "starwars",
        "whatever",
        "zaq12wsx",
        "asdfghjkl",
        "1q2w3e4r",
        "1qaz2wsx",
        "qazwsx",
        "changeme",
        "secret",
        "temppassword",
        "test1234",
        "testtest",
        "computer",
        "internet",
        "samsung",
        "google",
        "facebook",
        "photoshop",
        # Project-specific guesses: the service's own vocabulary is the
        # first thing someone tries against it.
        "telegram",
        "telegram123",
        "linkintelligence",
        "linkintel",
    }
)

MIN_PASSWORD_LENGTH = 8


def is_common_password(password: str) -> bool:
    """Whether this is a password an attacker would guess in their first attempts."""
    return password.strip().lower() in _COMMON_PASSWORDS


def rejection_reason(password: str) -> str | None:
    """Why this password is unacceptable, or None if it is fine.

    Returns a message meant to be shown to the person choosing it, so it
    says what to do rather than merely what failed.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if is_common_password(password):
        return (
            "this password appears in public breach lists and is among the first an "
            "attacker tries; choose something less predictable"
        )
    return None
