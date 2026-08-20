"""Collector account health: knowing which account is broken, and stopping it.

The behaviour this pins down is the one that was missing: an account that
cannot work — revoked session, wrong encryption key, banned — used to be
retried every hour forever, logging an error nobody read, while the
channels it owned quietly collected nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import MAX_CONSECUTIVE_FAILURES, reactivate, record_failure, record_success
from app.database import SessionLocal
from app.models import Channel, TelegramAccount, User, Workspace
from scripts.remove_account import RemovalRefused, remove_account
from tests.conftest import register_workspace


def _workspace_with_accounts(labels: list[str]) -> tuple[int, list[int]]:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Acct WS")
        db.add(workspace)
        db.flush()
        ids = []
        for label in labels:
            account = TelegramAccount(workspace_id=workspace.id, label=label, session_string="enc")
            db.add(account)
            db.flush()
            ids.append(account.id)
        db.commit()
        return workspace.id, ids
    finally:
        db.close()


def _account(account_id: int) -> TelegramAccount:
    db = SessionLocal()
    try:
        return db.get(TelegramAccount, account_id)
    finally:
        db.close()


# --- recording outcomes ----------------------------------------------------


def test_a_success_records_when_it_worked_and_clears_the_streak():
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        record_failure(db, account, "transient")
        record_success(db, account, links_collected=7)
    finally:
        db.close()

    account = _account(account_id)
    assert account.last_success_at is not None
    assert account.consecutive_failures == 0
    assert account.last_error is None
    assert account.links_collected == 7


def test_link_counts_accumulate_across_runs():
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        record_success(db, account, links_collected=3)
        record_success(db, account, links_collected=4)
    finally:
        db.close()

    assert _account(account_id).links_collected == 7


def test_a_failure_records_why_not_just_that():
    """ "This account is failing" without saying how is a dead end: a
    revoked session and a network blip need opposite responses."""
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        record_failure(db, db.get(TelegramAccount, account_id), "cannot connect: auth key revoked")
    finally:
        db.close()

    account = _account(account_id)
    assert account.consecutive_failures == 1
    assert "revoked" in account.last_error
    assert account.last_failure_at is not None
    assert account.is_active is True, "one failure must not disable an account"


def test_a_long_error_is_truncated_not_rejected():
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        record_failure(db, db.get(TelegramAccount, account_id), "x" * 5000)
    finally:
        db.close()

    assert len(_account(account_id).last_error) <= 300


def test_a_success_does_not_wrongly_clear_another_accounts_streak():
    _, (first, second) = _workspace_with_accounts(["a", "b"])
    db = SessionLocal()
    try:
        record_failure(db, db.get(TelegramAccount, first), "broken")
        record_success(db, db.get(TelegramAccount, second), links_collected=1)
    finally:
        db.close()

    assert _account(first).consecutive_failures == 1
    assert _account(second).consecutive_failures == 0


# --- automatic disable -----------------------------------------------------


def test_repeated_failures_eventually_disable_the_account():
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            assert record_failure(db, account, "still broken") is False
        assert record_failure(db, account, "still broken") is True
    finally:
        db.close()

    account = _account(account_id)
    assert account.is_active is False
    assert account.disabled_reason is not None
    assert str(MAX_CONSECUTIVE_FAILURES) in account.disabled_reason


def test_a_success_before_the_threshold_prevents_the_disable():
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
            record_failure(db, account, "blip")
        record_success(db, account, links_collected=0)
        record_failure(db, account, "another blip")
    finally:
        db.close()

    account = _account(account_id)
    assert account.is_active is True
    assert account.consecutive_failures == 1


def test_an_automatic_disable_is_distinguishable_from_a_human_one():
    """One needs investigating, the other was intended. A single is_active
    boolean cannot say which happened."""
    _, (auto, manual) = _workspace_with_accounts(["auto", "manual"])
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, auto)
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            record_failure(db, account, "broken")
        other = db.get(TelegramAccount, manual)
        other.is_active = False
        db.commit()
    finally:
        db.close()

    assert _account(auto).disabled_reason is not None
    assert _account(manual).disabled_reason is None


def test_reactivating_clears_the_streak_not_just_the_flag():
    """With the counter left where it was, the next single failure would
    disable the account again — which is not what re-enabling means."""
    _, (account_id,) = _workspace_with_accounts(["a"])
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            record_failure(db, account, "broken")
        reactivate(db, account)
    finally:
        db.close()

    account = _account(account_id)
    assert account.is_active is True
    assert account.consecutive_failures == 0
    assert account.disabled_reason is None
    assert account.last_error is None


# --- the API panel ---------------------------------------------------------


def _register_with_account(client: TestClient, email: str, label: str = "primary") -> int:
    register_workspace(client, email=email, workspace_name=f"WS {email}")
    db = SessionLocal()
    try:
        workspace_id = db.query(User).filter(User.email == email).one().workspace_id
        account = TelegramAccount(workspace_id=workspace_id, label=label, session_string="enc")
        db.add(account)
        db.commit()
        return account.id
    finally:
        db.close()


def test_the_account_panel_reports_health(client: TestClient):
    account_id = _register_with_account(client, "panel@example.com")
    db = SessionLocal()
    try:
        record_success(db, db.get(TelegramAccount, account_id), links_collected=12)
    finally:
        db.close()

    row = client.get("/channels/accounts").json()[0]

    assert row["links_collected"] == 12
    assert row["last_success_at"] is not None
    assert row["consecutive_failures"] == 0
    assert row["disabled_reason"] is None


def test_the_panel_never_exposes_the_session_string(client: TestClient):
    """The session string is a bearer credential for the Telegram account
    itself; nothing in the UI ever needs to read one back."""
    _register_with_account(client, "nosecret@example.com")

    body = client.get("/channels/accounts").text

    assert "session_string" not in body
    assert "enc" not in body


def test_the_default_account_is_credited_with_unassigned_channels(client: TestClient):
    """Channels naming no account fall to the default at collection time,
    so a panel that showed zero for it would contradict what runs."""
    _register_with_account(client, "unassigned@example.com")
    client.post("/channels", json={"tg_channel_id": "1", "username": "a"})

    row = client.get("/channels/accounts").json()[0]

    assert row["channel_count"] >= 1


def test_reactivating_through_the_api(client: TestClient):
    account_id = _register_with_account(client, "react@example.com")
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            record_failure(db, account, "broken")
    finally:
        db.close()
    assert client.get("/channels/accounts").json()[0]["is_active"] is False

    body = client.post(f"/channels/accounts/{account_id}/reactivate").json()

    assert body["is_active"] is True
    assert body["consecutive_failures"] == 0


def test_cannot_reactivate_another_workspaces_account(client: TestClient):
    victim = _register_with_account(client, "victim-acct@example.com")
    client.post("/auth/logout")

    register_workspace(client, email="attacker-acct@example.com", workspace_name="Attacker")
    assert client.post(f"/channels/accounts/{victim}/reactivate").status_code == 404


# --- removal ---------------------------------------------------------------


def test_removing_an_account_reassigns_its_channels_rather_than_failing():
    """Deleting an account that owns channels raises a ForeignKeyViolation.
    The channels are handed back to the default account instead, so they
    keep collecting rather than going quiet."""
    workspace_id, (first, second) = _workspace_with_accounts(["keep", "remove"])
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, account_id=second, tg_channel_id="c1"))
        db.add(Channel(workspace_id=workspace_id, account_id=second, tg_channel_id="c2"))
        db.commit()
        result = remove_account(db, workspace_id=workspace_id, label="remove")
    finally:
        db.close()

    assert result == {"channels_reassigned": 2, "accounts_removed": 1}

    db = SessionLocal()
    try:
        assert db.get(TelegramAccount, second) is None
        channels = db.query(Channel).filter(Channel.workspace_id == workspace_id).all()
        assert len(channels) == 2, "channels must survive their account"
        assert all(c.account_id is None for c in channels), "channels must fall back to the default"
    finally:
        db.close()


def test_removing_the_only_account_is_refused():
    """Collection would stop entirely — not a decision anyone makes by
    running a cleanup command."""
    workspace_id, _ = _workspace_with_accounts(["only"])
    db = SessionLocal()
    try:
        with pytest.raises(RemovalRefused, match="only collecting account"):
            remove_account(db, workspace_id=workspace_id, label="only")
    finally:
        db.close()


def test_removing_an_unknown_label_is_refused():
    workspace_id, _ = _workspace_with_accounts(["a", "b"])
    db = SessionLocal()
    try:
        with pytest.raises(RemovalRefused, match="no account labelled"):
            remove_account(db, workspace_id=workspace_id, label="nonexistent")
    finally:
        db.close()


def test_removal_does_not_reach_into_another_workspace():
    first_ws, _ = _workspace_with_accounts(["shared-label", "other"])
    second_ws, _ = _workspace_with_accounts(["shared-label", "other"])

    db = SessionLocal()
    try:
        remove_account(db, workspace_id=first_ws, label="shared-label")
    finally:
        db.close()

    db = SessionLocal()
    try:
        survivors = {
            a.label for a in db.query(TelegramAccount).filter(TelegramAccount.workspace_id == second_ws).all()
        }
    finally:
        db.close()
    assert "shared-label" in survivors


def test_a_dry_run_removes_nothing():
    workspace_id, (_, second) = _workspace_with_accounts(["keep", "remove"])
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, account_id=second, tg_channel_id="c1"))
        db.commit()
        result = remove_account(db, workspace_id=workspace_id, label="remove", dry_run=True)
    finally:
        db.close()

    assert result == {"channels_reassigned": 1, "accounts_removed": 0}
    assert _account(second) is not None


# --- the account cap -------------------------------------------------------


def test_adding_beyond_the_cap_is_refused(monkeypatch):
    from app.config import get_settings
    from scripts.add_account import add_account

    workspace_id, _ = _workspace_with_accounts(["a", "b"])
    monkeypatch.setattr(get_settings(), "max_accounts_per_workspace", 2)

    with pytest.raises(SystemExit, match="limit 2"):
        add_account(workspace_id, "c", "session")


def test_under_the_cap_still_works(monkeypatch):
    from app.config import get_settings
    from scripts.add_account import add_account

    workspace_id, _ = _workspace_with_accounts(["a"])
    monkeypatch.setattr(get_settings(), "max_accounts_per_workspace", 5)

    assert add_account(workspace_id, "b", "session") > 0


# --- the race the collector can actually hit (#104) ------------------------


def test_an_account_disabled_mid_run_does_not_corrupt_its_own_bookkeeping():
    """The real race: the collector holds a TelegramAccount object while an
    operator disables it through the API. The run must still record its
    outcome against the right row, and must not silently re-enable the
    account by writing a stale copy back.
    """
    _, (account_id,) = _workspace_with_accounts(["racer"])

    collector_db = SessionLocal()
    operator_db = SessionLocal()
    try:
        collector_view = collector_db.get(TelegramAccount, account_id)
        assert collector_view.is_active is True

        # The operator disables it while the collector is mid-run.
        operator_view = operator_db.get(TelegramAccount, account_id)
        operator_view.is_active = False
        operator_db.commit()

        # The collector finishes and records its success.
        record_success(collector_db, collector_view, links_collected=5)
    finally:
        collector_db.close()
        operator_db.close()

    account = _account(account_id)
    assert account.links_collected == 5, "the run's work must still be recorded"
    assert account.is_active is False, "recording a run must not resurrect a disabled account"


def test_a_disabled_account_is_not_selected_by_the_next_run():
    """The other half: once disabled, the collector must stop picking it
    up. Otherwise the disable is cosmetic."""
    from scripts.collect import _channels_for

    workspace_id, (account_id,) = _workspace_with_accounts(["racer"])
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, account_id=account_id, tg_channel_id="c1"))
        db.commit()

        account = db.get(TelegramAccount, account_id)
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            record_failure(db, account, "broken")

        active = (
            db.query(TelegramAccount)
            .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.is_active.is_(True))
            .all()
        )
        assert active == [], "a disabled account must not be selected"

        # Its channels are still there, waiting for a working account.
        assert len(_channels_for(db, workspace_id, account, is_default=True)) == 1
    finally:
        db.close()


def test_the_per_account_channel_cap_is_applied():
    """Telegram rate-limits per account, so one account carrying forty
    channels is asking for a FloodWait that costs the whole run."""
    from scripts.collect import _channels_for

    workspace_id, (account_id,) = _workspace_with_accounts(["capped"])
    db = SessionLocal()
    try:
        for i in range(8):
            db.add(Channel(workspace_id=workspace_id, account_id=account_id, tg_channel_id=f"c{i}"))
        db.commit()

        account = db.get(TelegramAccount, account_id)
        import os

        os.environ["COLLECTOR_MAX_CHANNELS_PER_ACCOUNT"] = "3"
        try:
            selected = _channels_for(db, workspace_id, account, is_default=False)
        finally:
            del os.environ["COLLECTOR_MAX_CHANNELS_PER_ACCOUNT"]
    finally:
        db.close()

    assert len(selected) == 3
    # A stable prefix by id, so the ones skipped are picked up next run
    # from their own watermarks rather than being random each time.
    assert [c.tg_channel_id for c in selected] == ["c0", "c1", "c2"]


def test_an_invalid_cap_falls_back_instead_of_crashing():
    import os

    from scripts.collect import DEFAULT_MAX_CHANNELS_PER_ACCOUNT, _max_channels_per_account

    os.environ["COLLECTOR_MAX_CHANNELS_PER_ACCOUNT"] = "not-a-number"
    try:
        assert _max_channels_per_account() == DEFAULT_MAX_CHANNELS_PER_ACCOUNT
    finally:
        del os.environ["COLLECTOR_MAX_CHANNELS_PER_ACCOUNT"]
