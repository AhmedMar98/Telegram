"""Tests for crypto module — AES-256-GCM + HMAC."""
import pytest

from app.crypto import (
    CryptoError,
    decrypt,
    derive_account_hmac,
    encrypt,
    generate_aes_key,
    generate_hmac_key,
    sign,
    verify,
)


def test_encrypt_decrypt_roundtrip():
    """Encrypted text decrypts back to original."""
    plaintext = "https://t.me/joinchat/abc123 secret invite"
    blob = encrypt(plaintext)
    assert blob != plaintext
    assert decrypt(blob) == plaintext


def test_encrypt_empty_string():
    assert encrypt("") == ""
    assert decrypt("") == ""


def test_decrypt_invalid_blob_raises():
    with pytest.raises(CryptoError):
        decrypt("not-valid-base64!!!")


def test_decrypt_tampered_blob_fails():
    """AES-GCM should detect tampering via authentication tag."""
    blob = encrypt("secret")
    # Flip a byte in the ciphertext
    tampered = blob[:-4] + ("0000" if blob[-4:] != "0000" else "1111")
    with pytest.raises(CryptoError):
        decrypt(tampered)


def test_associated_data_mismatch():
    """Decryption with wrong AAD fails."""
    blob = encrypt("secret", associated_data=b"context-A")
    with pytest.raises(CryptoError):
        decrypt(blob, associated_data=b"context-B")


def test_hmac_sign_verify():
    msg = b"GET /api/v1/links"
    account = "userbot_main"
    sig = sign(msg, account)
    assert verify(msg, account, sig) is True
    assert verify(msg, account, "0" * 64) is False  # wrong sig
    assert verify(b"tampered", account, sig) is False  # tampered msg
    assert verify(msg, "different_account", sig) is False  # different account


def test_per_account_keys_diverge():
    """Different accounts get different HMAC keys."""
    msg = b"hello"
    sig_a = sign(msg, "account_A")
    sig_b = sign(msg, "account_B")
    assert sig_a != sig_b


def test_key_generators():
    """Generated keys are 64 hex chars (32 bytes)."""
    assert len(generate_aes_key()) == 64
    assert len(generate_hmac_key()) == 64
    # Each call produces unique keys
    assert generate_aes_key() != generate_aes_key()
