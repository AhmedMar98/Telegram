"""Live collection: the path that stores a link the second it is posted.

Two things here are worth more than the rest of the file.

The first is ``test_the_watermark_is_never_touched``. Live ingestion runs
alongside a scheduled collector that resumes from
``Channel.last_message_id``, and if this path ever advanced that
watermark, a message the live listener never saw — a dropped socket, a
gap in Telegram's update stream — would fall between the two and be read
by nothing, ever. The loss would be silent, and silent loss is the one
kind you cannot notice and fix later. So the omission is pinned by a
test rather than left to a comment somebody edits out.

The second is ``canonical_id``. It decides whether an arriving message is
recognised as belonging to a followed channel at all, and it is wrong in
a way that is invisible: get it wrong and nothing errors, nothing logs,
links simply never appear. The tests below include the exact case that
makes the obvious implementation wrong.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app import live
from app.database import SessionLocal
from app.models import Channel, Link, Workspace


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    """Settings are cached process-wide, so a test that flips an
    environment variable would otherwise leak its answer into the next
    one — and into the rest of the suite, which shares this process."""
    live.get_settings.cache_clear()
    yield
    live.get_settings.cache_clear()


# --- recognising the channel a message came from ----------------------------


def test_the_two_spellings_of_one_channel_id_agree():
    """Telethon says -1001234567890; an operator pastes 1234567890."""
    assert live.canonical_id("-1001234567890") == live.canonical_id("1234567890")


def test_a_channel_whose_id_really_starts_with_100_still_matches():
    """The case that breaks the obvious implementation.

    Stripping a leading "100" from every id — rather than only from the
    ``-100`` peer prefix on negative ids — turns a channel genuinely
    numbered 1001234 into "1234" when it comes from the dashboard and
    "1001234" when it comes from Telethon. The two never meet, and the
    channel silently collects nothing forever.
    """
    assert live.canonical_id("-1001001234") == live.canonical_id("1001234") == "1001234"


def test_a_non_numeric_id_is_not_an_exception():
    """``tg_channel_id`` is free text on the model; a handle is a normal
    value there, not a reason to kill the update handler."""
    assert live.canonical_id("@somechannel") is None
    assert live.canonical_id(None) is None
    assert live.canonical_id("") is None


def test_handles_are_matched_case_insensitively_with_or_without_the_at():
    assert live.canonical_username("@DailyLinks") == live.canonical_username("dailylinks") == "dailylinks"
    assert live.canonical_username("   ") is None
    assert live.canonical_username(12345) is None


def test_a_channel_is_indexed_under_both_of_its_identities():
    """Which identity an event carries is not ours to choose: a public
    channel usually resolves a username, a private one never does."""
    channel = Channel(id=7, workspace_id=1, tg_channel_id="-1001234567890", username="DailyLinks")

    index = live.build_index([channel])

    assert live.lookup(index, -1001234567890, None) == 7
    assert live.lookup(index, None, "@dailylinks") == 7
    assert live.lookup(index, -100999, "someone_else") is None


def test_an_unmatched_chat_resolves_to_nothing():
    """The collecting account is a real login with its own DMs and groups.
    Most of what it hears is not this workspace's to store."""
    index = live.build_index([Channel(id=1, workspace_id=1, tg_channel_id="111", username=None)])

    assert live.lookup(index, -100222, None) is None


# --- storing a live message -------------------------------------------------


@pytest.fixture
def channel() -> Channel:
    db: Session = SessionLocal()
    try:
        workspace = Workspace(name="live-test")
        db.add(workspace)
        db.flush()
        row = Channel(workspace_id=workspace.id, tg_channel_id="-1005550001", username="livechan", is_active=True)
        db.add(row)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


class FakeButton:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeRow:
    def __init__(self, *buttons: FakeButton) -> None:
        self.buttons = list(buttons)


class FakeMarkup:
    def __init__(self, *rows: FakeRow) -> None:
        self.rows = list(rows)


class FakeChat:
    """A chat as an update carries it.

    ``kind`` shapes the object the way Telethon would: a broadcast channel
    carries the channel flags, a person carries ``first_name``. Discovery
    classifies by those attributes, so a fake without them is a chat whose
    type cannot be told — which is its own case, tested below.
    """

    def __init__(self, username: str | None = None, *, kind: str | None = None, title: str = "Discovered") -> None:
        self.username = username
        if kind == "channel":
            self.id = 5550002
            self.title = title
            self.broadcast = True
            self.megagroup = False
        elif kind == "group":
            self.id = 5550003
            self.title = title
            self.broadcast = False
            self.megagroup = True
        elif kind == "private":
            self.id = 5550004
            self.first_name = title
            self.last_name = None
            self.bot = False
            self.phone = None


class FakeEvent:
    """The parts of a Telethon NewMessage event this code actually reads."""

    def __init__(
        self,
        *,
        chat_id: int,
        text: str = "",
        message_id: int = 1,
        username: str | None = None,
        reply_markup: FakeMarkup | None = None,
        chat_kind: str | None = None,
        chat_title: str = "Discovered",
    ) -> None:
        self.chat_id = chat_id
        self.chat = FakeChat(username, kind=chat_kind, title=chat_title)
        self.id = message_id
        self.raw_text = text
        self.date = datetime.now(UTC)
        self.reply_markup = reply_markup
        self.forward = None

    @property
    def message(self):
        return self


def _stored_urls(workspace_id: int) -> list[str]:
    db = SessionLocal()
    try:
        return [link.url for link in db.query(Link).filter(Link.workspace_id == workspace_id).all()]
    finally:
        db.close()


def test_a_message_with_a_link_is_stored_immediately(channel):
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(chat_id=-1005550001, text="جديد: https://example.com/film-one")

    stored = asyncio.run(live.handle_event(event, index))

    assert stored == 1
    assert _stored_urls(channel.workspace_id) == ["https://example.com/film-one"]


def test_a_link_that_only_exists_on_a_button_is_still_captured(channel):
    """Channels routinely put the real link on an inline button and leave
    the body as marketing copy, so reading only the text misses precisely
    the link the post exists to share."""
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(
        chat_id=-1005550001,
        text="اضغط الزر للتحميل",
        reply_markup=FakeMarkup(FakeRow(FakeButton("https://example.com/from-button"))),
    )

    asyncio.run(live.handle_event(event, index))

    assert _stored_urls(channel.workspace_id) == ["https://example.com/from-button"]


def test_a_message_from_a_chat_of_no_identifiable_kind_stores_nothing(channel):
    """An update whose chat cannot be classified is not guessed at.

    Discovery registers dialogs by kind; an object that answers to none of
    the kinds is not filed under the most common one, it is left alone.
    """
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(chat_id=-100999999, text="https://example.com/not-ours")

    stored = asyncio.run(live.handle_event(event, index))

    assert stored == 0
    assert _stored_urls(channel.workspace_id) == []


def test_an_unfollowed_chat_is_registered_and_collected(channel, monkeypatch):
    """The gap discovery closes on the live path.

    Before it, the listener heard everything the account heard and stored
    only what somebody had typed into the dashboard first — so a channel
    joined this morning produced nothing until it was registered by hand.
    """
    monkeypatch.setenv("COLLECTOR_AUTO_DISCOVER", "true")
    monkeypatch.setenv("COLLECTOR_SCOPE", "all")
    live.get_settings.cache_clear()
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(
        chat_id=-1005550002,
        text="https://example.com/from-a-new-channel",
        chat_kind="channel",
        chat_title="Joined this morning",
    )

    stored = asyncio.run(live.handle_event(event, index))

    assert stored == 1
    assert _stored_urls(channel.workspace_id) == ["https://example.com/from-a-new-channel"]
    db = SessionLocal()
    try:
        row = (
            db.query(Channel)
            .filter(Channel.workspace_id == channel.workspace_id, Channel.title == "Joined this morning")
            .one()
        )
        assert row.kind == "channel"
        # Left to the default account on purpose: a passing message is not
        # a decision about which account should collect a dialog.
        assert row.account_id is None
    finally:
        db.close()
    live.get_settings.cache_clear()


def test_a_private_chat_is_left_alone_when_the_scope_excludes_it(channel, monkeypatch):
    """The setting that makes collecting personal conversations a decision
    rather than a surprise."""
    monkeypatch.setenv("COLLECTOR_AUTO_DISCOVER", "true")
    monkeypatch.setenv("COLLECTOR_SCOPE", "channel,group")
    live.get_settings.cache_clear()
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(chat_id=5550004, text="https://example.com/from-a-dm", chat_kind="private")

    stored = asyncio.run(live.handle_event(event, index))

    assert stored == 0
    assert _stored_urls(channel.workspace_id) == []
    live.get_settings.cache_clear()


def test_discovery_off_means_the_listener_stores_only_registered_dialogs(channel, monkeypatch):
    monkeypatch.setenv("COLLECTOR_AUTO_DISCOVER", "false")
    live.get_settings.cache_clear()
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(chat_id=-1005550002, text="https://example.com/not-ours", chat_kind="channel")

    stored = asyncio.run(live.handle_event(event, index))

    assert stored == 0
    assert _stored_urls(channel.workspace_id) == []
    live.get_settings.cache_clear()


def test_the_watermark_is_never_touched(channel):
    """The single most important assertion in this file.

    The scheduled collector resumes from ``last_message_id``. If this
    path advanced it, any message the listener missed would sit below the
    new watermark and never be scanned by anything again — silently. The
    overlap this leaves behind is deliberate: the unique constraint turns
    it into counted duplicates, which costs nothing and loses nothing.
    """
    index = live._Index(workspace_id=channel.workspace_id)
    event = FakeEvent(chat_id=-1005550001, text="https://example.com/a", message_id=9999)

    asyncio.run(live.handle_event(event, index))

    db = SessionLocal()
    try:
        assert db.get(Channel, channel.id).last_message_id == 0
    finally:
        db.close()


def test_the_same_link_arriving_twice_is_stored_once(channel):
    """What makes the overlap with the hourly collector safe."""
    index = live._Index(workspace_id=channel.workspace_id)

    asyncio.run(live.handle_event(FakeEvent(chat_id=-1005550001, text="https://example.com/dup"), index))
    asyncio.run(
        live.handle_event(FakeEvent(chat_id=-1005550001, text="https://example.com/dup", message_id=2), index)
    )

    assert _stored_urls(channel.workspace_id) == ["https://example.com/dup"]


def test_a_broken_event_is_counted_not_raised(channel):
    """Telethon runs handlers inside its own update loop. An exception
    escaping would be swallowed by the library and vanish from the status
    board, so it is caught here to make it countable."""
    index = live._Index(workspace_id=channel.workspace_id)

    class Exploding:
        chat_id = -1005550001
        chat = FakeChat("livechan")

        @property
        def message(self):
            raise RuntimeError("malformed update")

    stored = asyncio.run(live.handle_event(Exploding(), index))

    assert stored == 0
    assert "malformed update" in (live.state().last_error or "")


# --- startup decisions ------------------------------------------------------


def test_live_collection_is_off_unless_asked_for(monkeypatch):
    """Default-off is the whole reason existing deployments keep booting
    unchanged when this ships."""
    monkeypatch.delenv("LIVE_COLLECTOR_ENABLED", raising=False)
    live.get_settings.cache_clear()

    assert live.start() is None
    assert "off" in (live.state().reason or "")


def test_being_switched_on_but_unconfigured_says_which_variable_is_missing(monkeypatch):
    """ "Off" and "misconfigured" look identical from the dashboard
    otherwise, and only one of them is something to go and fix."""
    monkeypatch.setenv("LIVE_COLLECTOR_ENABLED", "true")
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("COLLECTOR_WORKSPACE_ID", "1")
    live.get_settings.cache_clear()
    try:
        assert live.start() is None
        assert "TG_API_ID" in (live.state().reason or "")
    finally:
        live.get_settings.cache_clear()


def test_a_non_numeric_api_id_is_refused_with_a_reason(monkeypatch):
    monkeypatch.setenv("LIVE_COLLECTOR_ENABLED", "true")
    monkeypatch.setenv("TG_API_ID", "not-a-number")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("COLLECTOR_WORKSPACE_ID", "1")
    live.get_settings.cache_clear()
    try:
        assert live.start() is None
        assert "must be numbers" in (live.state().reason or "")
    finally:
        live.get_settings.cache_clear()


def test_stopping_a_listener_that_never_started_is_not_an_error():
    """The lifespan calls stop() unconditionally on shutdown, including
    every deployment where live collection was never on."""
    asyncio.run(live.stop(None))
