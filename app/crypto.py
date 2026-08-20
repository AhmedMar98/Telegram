"""Field-level encryption for secrets that must live in the database.

Only one thing uses this today: ``TelegramAccount.session_string``. A
Telethon session string is a bearer credential — anyone holding it can act
as the collected account — so storing it in plaintext means a database
dump is an account takeover, not just a data leak. Everything else in this
project (passwords, session tokens) is already hashed and never needs to
be recovered; a session string is different because Telethon needs the
original value back to reconnect, so hashing is not an option and
symmetric encryption is used instead.

Fernet (AES-128-CBC + HMAC-SHA256, both keyed from ``FIELD_ENCRYPTION_KEY``)
was chosen over a raw cipher because it is authenticated: a tampered or
truncated ciphertext raises instead of silently decrypting to garbage.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

__all__ = ["encrypt_field", "decrypt_field", "InvalidToken"]


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().field_encryption_key.encode("utf-8"))


def encrypt_field(raw: str) -> str:
    """Encrypt a secret for storage. Returns an ASCII token safe for a Text column."""
    return _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_field(token: str) -> str:
    """Recover the original secret. Raises ``InvalidToken`` if the key is wrong
    or the stored value predates this module (plaintext, not a Fernet token)."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
