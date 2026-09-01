"""Collecting from the account's actual Telegram, not a hand-typed subset.

Before discovery, a row in ``channels`` had one origin: somebody typed it
into the dashboard. Every test here is about the consequences of removing
that restriction, and the two that matter most are not "it finds things":

- a dialog already known under a *different spelling of the same id* must
  not become a second row (that would split one channel's watermark and
  its links across two rows, silently);
- a dialog somebody switched off must not come back on because discovery
  saw it again.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.dialogs import DEFAULT_KIND, dialog_identity, dialog_kind, parse_scope
from app.models import Channel, TelegramAccount, Workspace
from app.timeutil import utcnow
from scripts import collect as collector

# --- fakes ------------------------------------------------------------------
#
# Shaped after the Telethon objects the code actually reads, not after the
# library's full type hierarchy: a broadcast channel and a megagroup are
# both `Channel` in the API and differ only by flags, which is exactly the
# distinction dialog_kind has to get right.


class FakeChannelEntity:
    def __init__(self, entity_id: int, title: str, username: str | None = None, *, megagroup: bool = False):
        self.id = entity_id
        self.title = title
        self.username = username
        self.broadcast = not megagroup
        self.megagroup = megagroup


class FakeBasicGroup:
    """A pre-supergroup chat: has a title, and none of the channel flags."""

    def __init__(self, entity_id: int, title: str):
        self.id = entity_id
        self.title = title


class FakeUser:
    def __init__(self, entity_id: int, first_name: str, username: str | None = None):
        self.id = entity_id
        self.first_name = first_name
        self.last_name = None
        self.username = username
        self.bot = False
        self.phone = None


class FakeDialog:
    """What ``iter_dialogs`` yields: a marked id plus the entity."""

    def __init__(self, dialog_id: int, entity: object):
        self.id = dialog_id
        self.entity = entity


class FakeDialogClient:
    def __init__(self, dialogs: list[FakeDialog], *, error: Exception | None = None):
        self._dialogs = dialogs
        self._error = error

    def iter_dialogs(self):
        dialogs = self._dialogs
        error = self._error

        async def _gen():
            if error is not None:
                raise error
            for dialog in dialogs:
                yield dialog

        return _gen()


@pytest.fixture
def workspace_and_account():
    db = SessionLocal()
    try:
        workspace = Workspace(name="Discovery WS")
        db.add(workspace)
        db.flush()
        account = TelegramAccount(workspace_id=workspace.id, label="primary", session_string="x")
        db.add(account)
        db.commit()
        return workspace.id, account.id
    finally:
        db.close()


def _discover(dialogs, workspace_id: int, account_id: int, *, error: Exception | None = None) -> int:
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        client = FakeDialogClient(dialogs, error=error)
        return asyncio.run(collector._discover_dialogs(client, db, workspace_id, account))
    finally:
        db.close()


def _rows(workspace_id: int) -> list[Channel]:
    db = SessionLocal()
    try:
        return db.query(Channel).filter(Channel.workspace_id == workspace_id).order_by(Channel.id).all()
    finally:
        db.close()


# --- classification ---------------------------------------------------------


def test_a_broadcast_channel_and_a_megagroup_are_told_apart():
    """Both are ``Channel`` in Telegram's API; only the flags differ, and
    a reader thinks of a megagroup as a group."""
    assert dialog_kind(FakeChannelEntity(1, "Broadcast")) == "channel"
    assert dialog_kind(FakeChannelEntity(2, "Supergroup", megagroup=True)) == "group"


def test_a_basic_group_and_a_person_are_told_apart():
    assert dialog_kind(FakeBasicGroup(3, "Old group")) == "group"
    assert dialog_kind(FakeUser(4, "Sara")) == "private"


def test_a_dialog_wrapper_is_classified_through_its_entity():
    assert dialog_kind(FakeDialog(-1001, FakeChannelEntity(1, "Broadcast"))) == "channel"
    assert dialog_kind(FakeDialog(5, FakeUser(5, "Sara"))) == "private"


def test_an_unrecognisable_object_is_not_guessed_at():
    """None, not a default kind: storing a dialog whose type is unknown
    under a type that happens to be common is how wrong data starts."""
    assert dialog_kind(object()) is None
    assert dialog_kind(None) is None


def test_the_identity_prefers_the_marked_id_telethon_will_report_back():
    tg_id, username, title = dialog_identity(FakeDialog(-1001234567890, FakeChannelEntity(1234567890, "PW", "pw")))

    assert tg_id == "-1001234567890"
    assert username == "pw"
    assert title == "PW"


def test_a_person_without_a_title_is_still_named():
    _, _, title = dialog_identity(FakeDialog(7, FakeUser(7, "Sara")))

    assert title == "Sara"


def test_scope_parsing():
    assert parse_scope("all") == frozenset({"channel", "group", "private", "bot"})
    assert parse_scope(None) == frozenset({"channel", "group", "private", "bot"})
    assert parse_scope("channel, group") == frozenset({"channel", "group"})
    # A scope naming nothing known narrows to channels rather than
    # collecting everything: the safe reading of an unreadable setting.
    assert parse_scope("nonsense") == frozenset({DEFAULT_KIND})


# --- discovery --------------------------------------------------------------


def test_every_kind_of_dialog_becomes_a_row(workspace_and_account, monkeypatch):
    workspace_id, account_id = workspace_and_account
    monkeypatch.setenv("COLLECTOR_SCOPE", "all")
    get_settings.cache_clear()

    created = _discover(
        [
            FakeDialog(-1001, FakeChannelEntity(1, "Broadcast", "bc")),
            FakeDialog(-1002, FakeChannelEntity(2, "Supergroup", megagroup=True)),
            FakeDialog(-3, FakeBasicGroup(3, "Old group")),
            FakeDialog(4, FakeUser(4, "Sara", "sara")),
        ],
        workspace_id,
        account_id,
    )

    assert created == 4
    assert {(row.kind, row.title) for row in _rows(workspace_id)} == {
        ("channel", "Broadcast"),
        ("group", "Supergroup"),
        ("group", "Old group"),
        ("private", "Sara"),
    }
    get_settings.cache_clear()


def test_scope_keeps_out_what_it_excludes(workspace_and_account, monkeypatch):
    workspace_id, account_id = workspace_and_account
    monkeypatch.setenv("COLLECTOR_SCOPE", "channel")
    get_settings.cache_clear()

    created = _discover(
        [
            FakeDialog(-1001, FakeChannelEntity(1, "Broadcast")),
            FakeDialog(4, FakeUser(4, "Sara")),
        ],
        workspace_id,
        account_id,
    )

    assert created == 1
    assert [row.kind for row in _rows(workspace_id)] == ["channel"]
    get_settings.cache_clear()


def test_discovery_can_be_switched_off_entirely(workspace_and_account, monkeypatch):
    workspace_id, account_id = workspace_and_account
    monkeypatch.setenv("COLLECTOR_AUTO_DISCOVER", "false")
    get_settings.cache_clear()

    assert _discover([FakeDialog(-1001, FakeChannelEntity(1, "Broadcast"))], workspace_id, account_id) == 0
    assert _rows(workspace_id) == []
    get_settings.cache_clear()


def test_a_channel_already_stored_under_the_other_spelling_is_not_duplicated(workspace_and_account):
    """The bug this exists to prevent, and the reason matching is done on
    canonicalised ids: the dashboard stores what a person pasted
    (``1234567890``), Telethon reports ``-1001234567890``. Two rows for one
    channel would split its watermark, so each run would re-read messages
    the other row had already seen and store them twice."""
    workspace_id, account_id = workspace_and_account
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, tg_channel_id="1234567890", title="Typed in by hand"))
        db.commit()
    finally:
        db.close()

    created = _discover(
        [FakeDialog(-1001234567890, FakeChannelEntity(1234567890, "Python Weekly", "pw"))],
        workspace_id,
        account_id,
    )

    rows = _rows(workspace_id)
    assert created == 0
    assert len(rows) == 1
    # The existing row is refreshed, not replaced: a renamed channel shows
    # its new name and its newly-claimed handle.
    assert rows[0].title == "Python Weekly"
    assert rows[0].username == "pw"


def test_discovery_never_re_enables_a_dialog_somebody_switched_off(workspace_and_account):
    workspace_id, account_id = workspace_and_account
    db = SessionLocal()
    try:
        db.add(
            Channel(
                workspace_id=workspace_id,
                tg_channel_id="-1001",
                title="Noisy channel",
                is_active=False,
            )
        )
        db.commit()
    finally:
        db.close()

    _discover([FakeDialog(-1001, FakeChannelEntity(1, "Noisy channel"))], workspace_id, account_id)

    assert _rows(workspace_id)[0].is_active is False


def test_the_watermark_of_a_known_dialog_survives_discovery(workspace_and_account):
    workspace_id, account_id = workspace_and_account
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, tg_channel_id="-1001", title="Known", last_message_id=500))
        db.commit()
    finally:
        db.close()

    _discover([FakeDialog(-1001, FakeChannelEntity(1, "Known"))], workspace_id, account_id)

    assert _rows(workspace_id)[0].last_message_id == 500


def test_one_pass_is_capped(workspace_and_account, monkeypatch):
    workspace_id, account_id = workspace_and_account
    monkeypatch.setenv("COLLECTOR_MAX_DIALOGS", "2")
    get_settings.cache_clear()

    created = _discover(
        [FakeDialog(-(1000 + n), FakeChannelEntity(n, f"Channel {n}")) for n in range(10)],
        workspace_id,
        account_id,
    )

    assert created == 2
    get_settings.cache_clear()


def test_a_failure_mid_discovery_is_not_a_failure_of_the_run(workspace_and_account):
    """Discovery is additive. An account whose dialog list cannot be read
    can still collect the channels already registered, so the failure is
    logged and the run continues rather than losing everything."""
    workspace_id, account_id = workspace_and_account

    assert _discover([], workspace_id, account_id, error=RuntimeError("flood wait")) == 0
    assert _rows(workspace_id) == []


# --- fair rotation ----------------------------------------------------------


def test_never_collected_dialogs_are_read_before_stale_ones(workspace_and_account, monkeypatch):
    """With hundreds of discovered dialogs and a per-run cap, ordering by
    id would mean everything past the cap is never read at all. Ordering
    by age of last collection makes the cap a rotation instead."""
    workspace_id, account_id = workspace_and_account
    monkeypatch.setenv("COLLECTOR_MAX_CHANNELS_PER_ACCOUNT", "2")

    db = SessionLocal()
    try:
        now = utcnow()
        db.add(Channel(workspace_id=workspace_id, tg_channel_id="-1", title="just read", last_collected_at=now))
        db.add(
            Channel(
                workspace_id=workspace_id,
                tg_channel_id="-2",
                title="read long ago",
                last_collected_at=now.replace(year=now.year - 1),
            )
        )
        db.add(Channel(workspace_id=workspace_id, tg_channel_id="-3", title="never read"))
        db.commit()

        account = db.get(TelegramAccount, account_id)
        selected = collector._channels_for(db, workspace_id, account, is_default=True)

        assert [row.title for row in selected] == ["never read", "read long ago"]
    finally:
        db.close()


def test_reading_a_dialog_stamps_when_it_was_read(workspace_and_account):
    """The stamp the rotation depends on. Written even when the dialog had
    nothing new, because the question it answers is "when did anything
    last look here" — stamping only non-empty runs would park every quiet
    dialog permanently at the front of the queue."""

    class _EmptyClient:
        async def get_entity(self, ref):
            return f"entity:{ref}"

        def iter_messages(self, entity, **kwargs):
            async def _gen():
                return
                yield  # pragma: no cover - an empty async generator

            return _gen()

    workspace_id, _ = workspace_and_account
    db = SessionLocal()
    try:
        row = Channel(workspace_id=workspace_id, tg_channel_id="-1001", title="Quiet")
        db.add(row)
        db.commit()
        assert row.last_collected_at is None

        asyncio.run(collector._collect_channel(_EmptyClient(), db, row))

        assert row.last_collected_at is not None
    finally:
        db.close()


def test_a_malformed_stored_session_fails_only_its_own_account(workspace_and_account, monkeypatch):
    """Per-account isolation, at the one point it used to leak.

    ``StringSession()`` *parses* the stored string and raises on a
    malformed one. While that construction sat outside the try block, the
    exception escaped the per-account handler and ended the entire run —
    for exactly the account most likely to be broken. Discovery exposed
    it: before, an account holding no channels returned early and never
    reached the constructor at all.
    """
    workspace_id, account_id = workspace_and_account
    db = SessionLocal()
    try:
        account = db.get(TelegramAccount, account_id)
        account.session_string = "not-a-real-fernet-token"
        db.commit()

        # Decryption fails first for this row, so drive the narrower case
        # directly: a session that decrypts to something Telethon rejects.
        monkeypatch.setattr(collector, "decrypt_field", lambda _: "definitely not a session string")

        links, read = asyncio.run(
            collector._collect_with_account(
                db, account, [], collector._pacer(), api_id=1, api_hash="h", is_default=True
            )
        )

        assert (links, read) == (0, 0)
        assert account.consecutive_failures == 1
        assert "cannot connect" in (account.last_error or "")
    finally:
        db.close()


# --- bots are their own kind ----------------------------------------------
#
# The claim under review was "Telegram models a bot chat as a User, so bots
# cannot be separated". That is true of the type and false of the entity: a
# Telethon User carries a `bot` boolean, and the classifier below was
# already testing for the attribute's existence before discarding what it
# said.


class _Entity:
    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


def test_a_bot_chat_is_classified_as_a_bot_not_a_private_chat():
    from app.dialogs import dialog_kind

    assert dialog_kind(_Entity(is_user=True, entity=_Entity(bot=True))) == "bot"


def test_a_person_is_still_a_private_chat():
    from app.dialogs import dialog_kind

    assert dialog_kind(_Entity(is_user=True, entity=_Entity(bot=False))) == "private"


def test_a_peer_that_cannot_prove_it_is_a_bot_is_treated_as_private():
    """Fail towards the more cautious of the two settings: `private` is the
    privacy-sensitive scope, and misfiling a person's chat as a high-volume
    bot feed is the worse of the two errors."""
    from app.dialogs import dialog_kind

    assert dialog_kind(_Entity(is_user=True, entity=_Entity())) == "private"


def test_bot_is_a_scope_that_can_be_selected_on_its_own():
    from app.dialogs import parse_scope

    assert parse_scope("bot") == frozenset({"bot"})


def test_an_existing_private_row_is_upgraded_to_bot_on_rediscovery(workspace_and_account):
    """The bug that would have made this feature look finished.

    `register_dialog` only ever overwrote the *default* kind, so every bot
    chat already on disk — all of them, since "bot" did not exist until now
    — would have stayed filed as `private` forever. New rows would classify
    correctly, which is exactly what anyone testing the feature would check.
    """
    from app.dialogs import register_dialog
    from app.models import Channel

    workspace_id, _ = workspace_and_account
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, tg_channel_id="777", kind="private", username="helperbot"))
        db.commit()

        register_dialog(
            db,
            workspace_id=workspace_id,
            account_id=None,
            kind="bot",
            tg_id="777",
            username="helperbot",
            title="Helper",
        )
        db.commit()

        row = db.query(Channel).filter(Channel.tg_channel_id == "777").one()
        assert row.kind == "bot", "an existing private row was never reclassified"
    finally:
        db.close()


def test_a_bot_row_is_never_demoted_back_to_private(workspace_and_account):
    """Upgrades go one way. Losing the distinction is not a refinement, and
    a single ambiguous observation must not undo a confident one."""
    from app.dialogs import register_dialog
    from app.models import Channel

    workspace_id, _ = workspace_and_account
    db = SessionLocal()
    try:
        db.add(Channel(workspace_id=workspace_id, tg_channel_id="888", kind="bot", username="knownbot"))
        db.commit()

        register_dialog(
            db,
            workspace_id=workspace_id,
            account_id=None,
            kind="private",
            tg_id="888",
            username="knownbot",
            title="Known",
        )
        db.commit()

        assert db.query(Channel).filter(Channel.tg_channel_id == "888").one().kind == "bot"
    finally:
        db.close()
