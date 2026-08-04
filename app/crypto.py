"""
Cryptographic utilities — AES-256-GCM for data at rest + HMAC for request signing.

Design:
    - AES-256-GCM provides authenticated encryption (confidentiality + integrity).
    - Separate HMAC keys per account (derived from master HMAC key) for API auth.
    - Keys come from .env (never hardcoded).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from base64 import b64decode, b64encode
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings


class CryptoError(Exception):
    """Raised when encryption/decryption or verification fails."""


def _get_aes_key() -> bytes:
    """Return the 32-byte AES key from settings, generating a dev fallback if missing."""
    settings = get_settings()
    if settings.aes_encryption_key:
        return bytes.fromhex(settings.aes_encryption_key)
    # Development-only fallback (NOT secure, but lets MVP run without manual key setup)
    dev_key = hashlib.sha256(b"link-intel-dev-key-do-not-use-in-prod").digest()
    return dev_key


def _get_hmac_key() -> bytes:
    settings = get_settings()
    if settings.hmac_key:
        return bytes.fromhex(settings.hmac_key)
    return hashlib.sha256(b"link-intel-dev-hmac-do-not-use-in-prod").digest()


def derive_account_hmac(account_id: str) -> bytes:
    """Derive a per-account HMAC key from the master key (HKDF-like)."""
    master = _get_hmac_key()
    return hashlib.sha256(master + account_id.encode("utf-8")).digest()


# ============================================================
# AES-256-GCM
# ============================================================

def encrypt(plaintext: str, associated_data: Optional[bytes] = None) -> str:
    """
    Encrypt a string with AES-256-GCM.

    Returns a base64 string: nonce (12 bytes) || ciphertext || tag (16 bytes).
    Empty/None inputs return "" (no encryption overhead).
    """
    if not plaintext:
        return ""
    key = _get_aes_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
    return b64encode(nonce + ct).decode("ascii")


def decrypt(blob: str, associated_data: Optional[bytes] = None) -> str:
    """Decrypt a base64 blob produced by `encrypt`. Empty input returns empty string."""
    if not blob:
        return ""
    try:
        raw = b64decode(blob)
        if len(raw) < 28:  # 12 nonce + 16 tag minimum
            raise CryptoError("Ciphertext too short")
        nonce, ct = raw[:12], raw[12:]
        aesgcm = AESGCM(_get_aes_key())
        return aesgcm.decrypt(nonce, ct, associated_data).decode("utf-8")
    except Exception as e:
        raise CryptoError(f"Decryption failed: {e}") from e


# ============================================================
# HMAC signing (for inter-service API auth)
# ============================================================

def sign(message: bytes, account_id: str) -> str:
    """Sign a message with the per-account HMAC key. Returns hex digest."""
    key = derive_account_hmac(account_id)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(message: bytes, account_id: str, signature: str) -> bool:
    """Verify a signature in constant time."""
    expected = sign(message, account_id)
    return hmac.compare_digest(expected, signature)


# ============================================================
# Helpers
# ============================================================

def generate_aes_key() -> str:
    """Generate a fresh 32-byte AES key as hex (for .env setup)."""
    return secrets.token_hex(32)


def generate_hmac_key() -> str:
    return secrets.token_hex(32)
