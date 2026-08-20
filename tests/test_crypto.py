"""Round-trip and tamper-detection tests for app/crypto.py.

This is the module that turns "session strings are stored in cleartext"
from a real vulnerability into a mitigated one, so it gets tested on its
own — independent of the collector that happens to be its only caller
today — the way every other security-relevant primitive in this project
(app/security.py) already is.
"""

from __future__ import annotations

import pytest

from app.crypto import InvalidToken, decrypt_field, encrypt_field


def test_encrypted_value_differs_from_the_original():
    token = encrypt_field("a-telethon-session-string")
    assert token != "a-telethon-session-string"


def test_decrypt_recovers_the_original_value():
    original = "1BVtsOKKBu1J8p3q...a Telethon StringSession is long and opaque"
    assert decrypt_field(encrypt_field(original)) == original


def test_empty_string_round_trips():
    assert decrypt_field(encrypt_field("")) == ""


def test_two_encryptions_of_the_same_value_are_not_identical():
    # Fernet includes a random nonce and timestamp per call — ciphertext
    # equality across calls would mean the nonce is not actually random.
    assert encrypt_field("same input") != encrypt_field("same input")


def test_a_value_that_was_never_encrypted_is_rejected_not_silently_accepted():
    """Guards the pre-fix rows: plaintext left over from before this module
    existed must raise loudly instead of `decrypt_field` returning garbage
    that looks like a usable session string."""
    with pytest.raises(InvalidToken):
        decrypt_field("this-was-never-a-fernet-token")
