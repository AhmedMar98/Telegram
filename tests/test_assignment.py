"""Source assignment (§43.6), and the access constraint that shapes it.

The test that matters most here is
``test_a_private_channel_is_never_moved_to_another_account``. Balancing by
load alone is easy and produces an assignment that looks even and collects
nothing, because a userbot cannot read a private dialog it is not in. That
test is the difference between a working engine and a plausible one.
"""

import pytest

from app.assignment import (
    apply_assignments,
    collectable_channels,
    is_portable,
    plan_assignments,
)
from app.database import SessionLocal
from app.models import Channel, TelegramAccount, Workspace
from tests.conftest import register_workspace


@pytest.fixture()
def workspace_id() -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Assign")
        db.add(workspace)
        db.commit()
        return workspace.id
    finally:
        db.close()


def _account(db, workspace_id: int, label: str, *, active: bool = True) -> TelegramAccount:
    # A disabled account has to say which kind of disabled it is: the
    # database refuses "not active" without a state that explains it.
    account = TelegramAccount(
        workspace_id=workspace_id,
        label=label,
        session_string="x",
        is_active=active,
        state=TelegramAccount.ACTIVE if active else TelegramAccount.DISABLED,
    )
    db.add(account)
    db.flush()
    return account


def _channel(db, workspace_id: int, tg_id: str, *, username=None, account_id=None, **kwargs) -> Channel:
    channel = Channel(
        workspace_id=workspace_id,
        tg_channel_id=tg_id,
        username=username,
        account_id=account_id,
        **kwargs,
    )
    db.add(channel)
    db.flush()
    return channel


def test_unassigned_public_channels_spread_across_accounts(workspace_id):
    db = SessionLocal()
    try:
        first = _account(db, workspace_id, "A")
        second = _account(db, workspace_id, "B")
        channels = [_channel(db, workspace_id, f"-100{n}", username=f"c{n}") for n in range(4)]
        db.commit()

        changes, report = plan_assignments(channels, [first, second], capacity=100)
        assert report.moved == 4
        assert sorted(report.per_account.values()) == [2, 2]
        assert set(changes.values()) == {first.id, second.id}
    finally:
        db.close()


def test_a_channel_on_a_healthy_account_is_left_alone(workspace_id):
    """Stability: a move costs the new account a dialog it may not open."""
    db = SessionLocal()
    try:
        first = _account(db, workspace_id, "A")
        second = _account(db, workspace_id, "B")
        channels = [
            _channel(db, workspace_id, f"-100{n}", username=f"k{n}", account_id=first.id) for n in range(5)
        ]
        db.commit()

        changes, report = plan_assignments(channels, [first, second], capacity=100)
        assert changes == {}
        assert report.kept == 5
        assert report.moved == 0
    finally:
        db.close()


def test_a_private_channel_is_never_moved_to_another_account(workspace_id):
    """Access, not load. Sabotage: drop the ``is_portable`` guard in
    ``plan_assignments`` and this channel is handed to an account that
    cannot resolve it — an assignment that looks even and reads nothing."""
    db = SessionLocal()
    try:
        dead = _account(db, workspace_id, "dead", active=False)
        alive = _account(db, workspace_id, "alive")
        private = _channel(db, workspace_id, "-1001111", username=None, account_id=dead.id)
        db.commit()

        assert not is_portable(private)
        changes, report = plan_assignments([private], [alive], capacity=100)
        assert changes == {}
        assert report.stranded == ["-1001111"]
        assert report.needs_attention
    finally:
        db.close()


def test_a_public_channel_moves_off_a_dead_account(workspace_id):
    """The counterpart: a @username resolves for any account, so it moves."""
    db = SessionLocal()
    try:
        dead = _account(db, workspace_id, "dead", active=False)
        alive = _account(db, workspace_id, "alive")
        public = _channel(db, workspace_id, "-1002222", username="public_one", account_id=dead.id)
        db.commit()

        changes, report = plan_assignments([public], [alive], capacity=100)
        assert changes == {public.id: alive.id}
        assert report.stranded == []
    finally:
        db.close()


def test_capacity_is_respected_and_the_excess_is_reported(workspace_id):
    db = SessionLocal()
    try:
        only = _account(db, workspace_id, "only")
        channels = [_channel(db, workspace_id, f"-100{n}", username=f"o{n}") for n in range(5)]
        db.commit()

        _, report = plan_assignments(channels, [only], capacity=3)
        assert report.moved == 3
        assert len(report.overflow) == 2
        assert report.needs_attention
    finally:
        db.close()


def test_with_no_active_accounts_everything_is_reported_stranded(workspace_id):
    db = SessionLocal()
    try:
        channels = [_channel(db, workspace_id, "-1003333", username="lonely")]
        db.commit()

        changes, report = plan_assignments(channels, [], capacity=100)
        assert changes == {}
        assert report.stranded == ["lonely"]
    finally:
        db.close()


def test_synthetic_channels_are_not_assignable(workspace_id):
    """``manual`` and ``import:*`` stand for no Telegram dialog.

    Sabotage: remove the ``is_synthetic`` filter and the manual bucket is
    handed to an account, which then spends a Telegram round trip per run
    failing to resolve a channel named "manual".
    """
    db = SessionLocal()
    try:
        _account(db, workspace_id, "A")
        _channel(db, workspace_id, "manual", username=None)
        _channel(db, workspace_id, "import:bookmarks.html", username=None)
        real = _channel(db, workspace_id, "-1004444", username="real_one")
        db.commit()

        assert [row.id for row in collectable_channels(db, workspace_id)] == [real.id]
    finally:
        db.close()


def test_public_sources_are_left_to_the_scraper(workspace_id):
    """A public-source row carries its own watermark; a userbot reading it
    would move that watermark past posts the scraper never fetched."""
    db = SessionLocal()
    try:
        _account(db, workspace_id, "A")
        _channel(db, workspace_id, "-1005555", username="scraped", source="public")
        db.commit()

        assert collectable_channels(db, workspace_id) == []
    finally:
        db.close()


def test_apply_writes_the_plan_and_commits(workspace_id):
    db = SessionLocal()
    try:
        account = _account(db, workspace_id, "A")
        channel = _channel(db, workspace_id, "-1006666", username="written")
        db.commit()
        channel_id, account_id = channel.id, account.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        report = apply_assignments(db, workspace_id)
        assert report.moved == 1
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.get(Channel, channel_id).account_id == account_id
    finally:
        db.close()


# --- the HTTP surface ------------------------------------------------------


def test_the_assignment_view_changes_nothing(client):
    register_workspace(client, email="assign@example.com", workspace_name="AssignAPI")
    client.post("/channels", json={"tg_channel_id": "-1007777", "username": "viewed"})

    body = client.get("/channels/assignments").json()
    assert body["moved"] == 0, "a GET must not rewrite rows"
    assert body["capacity_per_account"] >= 1

    after = client.get("/channels").json()
    assert after[0]["account_id"] is None


def test_rebalancing_requires_permission_to_manage_collection(client):
    """An agent reads leads; it does not move sources between accounts."""
    from app.database import SessionLocal as Session
    from app.models import User

    register_workspace(client, email="agent@example.com", workspace_name="AgentWs")
    db = Session()
    try:
        user = db.query(User).filter(User.email == "agent@example.com").one()
        user.role = "agent"
        db.commit()
    finally:
        db.close()

    assert client.post("/channels/assignments/rebalance").status_code == 403
    assert client.get("/channels/assignments").status_code == 403
