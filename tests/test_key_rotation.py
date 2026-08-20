"""Rotating FIELD_ENCRYPTION_KEY without stranding stored secrets.

Rotation is the kind of operation that gets run once, under pressure,
against real data. The properties worth pinning are therefore about what
happens when it goes wrong: a resumed run, a wrong old key, and a
half-migrated table.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.database import SessionLocal
from app.models import TelegramAccount, Workspace
from scripts.rotate_encryption_key import rotate

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()
SESSION = "1BVtsOK" + "x" * 130


@pytest.fixture
def workspace_id() -> int:
    db = SessionLocal()
    try:
        ws = Workspace(name="Rotation WS")
        db.add(ws)
        db.commit()
        return ws.id
    finally:
        db.close()


def _add(workspace_id: int, label: str, secret: str, key: str) -> int:
    db = SessionLocal()
    try:
        account = TelegramAccount(
            workspace_id=workspace_id,
            label=label,
            session_string=Fernet(key.encode()).encrypt(secret.encode()).decode(),
        )
        db.add(account)
        db.commit()
        return account.id
    finally:
        db.close()


def _stored(account_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(TelegramAccount, account_id).session_string
    finally:
        db.close()


def test_rotation_re_encrypts_under_the_new_key(workspace_id):
    account_id = _add(workspace_id, "primary", SESSION, OLD_KEY)

    summary = rotate(OLD_KEY, NEW_KEY, dry_run=False)

    assert summary.rotated == 1
    stored = _stored(account_id)
    assert Fernet(NEW_KEY.encode()).decrypt(stored.encode()).decode() == SESSION


def test_the_plaintext_secret_survives_rotation_unchanged(workspace_id):
    """The whole point: the account still works afterwards."""
    account_id = _add(workspace_id, "primary", SESSION, OLD_KEY)
    rotate(OLD_KEY, NEW_KEY, dry_run=False)

    recovered = Fernet(NEW_KEY.encode()).decrypt(_stored(account_id).encode()).decode()

    assert recovered == SESSION


def test_dry_run_changes_nothing(workspace_id):
    account_id = _add(workspace_id, "primary", SESSION, OLD_KEY)
    before = _stored(account_id)

    summary = rotate(OLD_KEY, NEW_KEY, dry_run=True)

    assert summary.rotated == 1
    assert _stored(account_id) == before


def test_rotation_is_idempotent(workspace_id):
    """A resumed run must not double-encrypt or fail."""
    account_id = _add(workspace_id, "primary", SESSION, OLD_KEY)
    rotate(OLD_KEY, NEW_KEY, dry_run=False)

    second = rotate(OLD_KEY, NEW_KEY, dry_run=False)

    assert second.rotated == 0
    assert second.already_rotated == 1
    assert Fernet(NEW_KEY.encode()).decrypt(_stored(account_id).encode()).decode() == SESSION


def test_a_partially_rotated_table_completes_cleanly(workspace_id):
    """Simulates an interrupted run: one row already moved, one not."""
    done = _add(workspace_id, "already-done", SESSION, NEW_KEY)
    pending = _add(workspace_id, "still-old", SESSION, OLD_KEY)

    summary = rotate(OLD_KEY, NEW_KEY, dry_run=False)

    assert summary.rotated == 1
    assert summary.already_rotated == 1
    for account_id in (done, pending):
        assert Fernet(NEW_KEY.encode()).decrypt(_stored(account_id).encode()).decode() == SESSION


def test_a_row_readable_by_neither_key_aborts_the_whole_run(workspace_id):
    """Refusing beats leaving the table split across two keys."""
    good = _add(workspace_id, "good", SESSION, OLD_KEY)
    db = SessionLocal()
    try:
        db.add(TelegramAccount(workspace_id=workspace_id, label="corrupt", session_string="not-a-token"))
        db.commit()
    finally:
        db.close()
    before = _stored(good)

    with pytest.raises(SystemExit, match="cannot be decrypted with either key"):
        rotate(OLD_KEY, NEW_KEY, dry_run=False)

    # The good row must be untouched — nothing was committed.
    assert _stored(good) == before


def test_empty_table_is_a_no_op():
    summary = rotate(OLD_KEY, NEW_KEY, dry_run=False)

    assert summary.total == 0
    assert summary.rotated == 0
