"""Registering an additional collecting account.

The script is the only supported way to add a second Telegram account, so
its guard rails matter: a session string must never be stored in the
clear, a truncated paste must be rejected before it becomes a row that
fails silently at collection time, and a duplicate label must not create
two indistinguishable accounts.
"""

from __future__ import annotations

import pytest

from app.crypto import decrypt_field
from app.database import SessionLocal
from app.models import TelegramAccount, Workspace
from scripts.add_account import MIN_SESSION_LENGTH, add_account, main

SESSION = "1BVtsOK" + "x" * 130


@pytest.fixture
def workspace_id() -> int:
    db = SessionLocal()
    try:
        ws = Workspace(name="Add Account WS")
        db.add(ws)
        db.commit()
        return ws.id
    finally:
        db.close()


def _stored(account_id: int) -> TelegramAccount:
    db = SessionLocal()
    try:
        return db.get(TelegramAccount, account_id)
    finally:
        db.close()


def test_session_string_is_encrypted_before_storage(workspace_id):
    account_id = add_account(workspace_id, "second", SESSION)

    account = _stored(account_id)
    assert account.session_string != SESSION
    assert decrypt_field(account.session_string) == SESSION


def test_new_account_is_active_and_labelled(workspace_id):
    account = _stored(add_account(workspace_id, "second", SESSION))

    assert account.label == "second"
    assert account.is_active is True
    assert account.workspace_id == workspace_id


def test_duplicate_label_in_the_same_workspace_is_refused(workspace_id):
    add_account(workspace_id, "second", SESSION)

    with pytest.raises(SystemExit, match="already has an account labelled"):
        add_account(workspace_id, "second", SESSION)


def test_the_same_label_is_fine_in_a_different_workspace(workspace_id):
    db = SessionLocal()
    try:
        other = Workspace(name="Other WS")
        db.add(other)
        db.commit()
        other_id = other.id
    finally:
        db.close()

    add_account(workspace_id, "second", SESSION)
    account = _stored(add_account(other_id, "second", SESSION))

    assert account.workspace_id == other_id


def test_unknown_workspace_is_refused(workspace_id):
    with pytest.raises(SystemExit, match="no workspace with id"):
        add_account(workspace_id + 9999, "second", SESSION)


def test_cli_refuses_a_missing_session_string(monkeypatch, workspace_id):
    monkeypatch.delenv("TG_SESSION_STRING", raising=False)
    monkeypatch.setattr("sys.argv", ["add_account.py", "--workspace", str(workspace_id), "--label", "second"])

    with pytest.raises(SystemExit, match="TG_SESSION_STRING is not set"):
        main()


def test_cli_refuses_a_truncated_session_string(monkeypatch, workspace_id):
    monkeypatch.setenv("TG_SESSION_STRING", "x" * (MIN_SESSION_LENGTH - 1))
    monkeypatch.setattr("sys.argv", ["add_account.py", "--workspace", str(workspace_id), "--label", "second"])

    with pytest.raises(SystemExit, match="too short to be a real session"):
        main()


def test_cli_never_prints_the_session_string(monkeypatch, capsys, workspace_id):
    monkeypatch.setenv("TG_SESSION_STRING", SESSION)
    monkeypatch.setattr("sys.argv", ["add_account.py", "--workspace", str(workspace_id), "--label", "second"])

    main()

    printed = capsys.readouterr().out
    assert SESSION not in printed
    assert "registered account" in printed
