"""Collector tests driven by a fake Telethon client.

The collector is the component that actually writes data, so its edge
cases (duplicates, bad channel ids, rate limits, watermarking) are worth
covering without ever touching the real Telegram network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from telethon.errors import FloodWaitError

from app.crypto import decrypt_field
from app.database import SessionLocal
from app.models import Channel, Link, TelegramAccount, Workspace
from scripts import collect as collector


class FakeMessage:
    def __init__(self, message_id: int, text: str, date: datetime | None = None):
        self.id = message_id
        self.raw_text = text
        self.date = date or datetime.now(UTC)


class FakeClient:
    """Minimal stand-in exposing only what the collector actually calls."""

    def __init__(
        self, messages: list[FakeMessage], *, entity_error: Exception | None = None, flood_after: int | None = None
    ):
        self._messages = messages
        self._entity_error = entity_error
        self._flood_after = flood_after
        self.requested_entities: list[object] = []
        self.iter_kwargs: dict = {}

    async def get_entity(self, ref):
        self.requested_entities.append(ref)
        if self._entity_error is not None:
            raise self._entity_error
        return f"entity:{ref}"

    def iter_messages(self, entity, **kwargs):
        self.iter_kwargs = kwargs
        min_id = kwargs.get("min_id", 0)
        limit = kwargs.get("limit")
        selected = [m for m in self._messages if m.id > min_id]
        selected.sort(key=lambda m: m.id, reverse=not kwargs.get("reverse", False))
        if limit is not None:
            selected = selected[:limit]
        flood_after = self._flood_after

        async def _gen():
            for index, message in enumerate(selected):
                if flood_after is not None and index >= flood_after:
                    raise FloodWaitError(request=None)
                yield message

        return _gen()


@pytest.fixture
def workspace_and_channel():
    db = SessionLocal()
    try:
        workspace = Workspace(name="Collector WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="1001", username="testchan", title="Test")
        db.add(channel)
        db.commit()
        return workspace.id, channel.id
    finally:
        db.close()


def _run_collect(client, channel_id: int) -> int:
    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        return asyncio.run(collector._collect_channel(client, db, channel))
    finally:
        db.close()


def _stored_urls(workspace_id: int) -> list[str]:
    db = SessionLocal()
    try:
        return [
            link.url for link in db.query(Link).filter(Link.workspace_id == workspace_id).order_by(Link.id).all()
        ]
    finally:
        db.close()


def test_collects_and_classifies_links(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    client = FakeClient([FakeMessage(5, "تحميل التطبيق https://example.com/app.apk الآن")])

    assert _run_collect(client, channel_id) == 1

    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.workspace_id == workspace_id).one()
        assert link.url == "https://example.com/app.apk"
        assert link.category == "software_apps"
        assert link.classified_by == "rules-v2"  # the classifier version, since §43
        assert link.domain == "example.com"
        assert link.message_id == 5
    finally:
        db.close()


def test_trailing_punctuation_is_stripped_before_storage(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    client = FakeClient([FakeMessage(1, "زوروا https://example.com/book.pdf، وشكراً")])

    assert _run_collect(client, channel_id) == 1
    assert _stored_urls(workspace_id) == ["https://example.com/book.pdf"]


def test_duplicate_url_does_not_discard_earlier_links(workspace_and_channel):
    """A duplicate must roll back only itself, not the whole batch.

    This is the regression guard for the savepoint fix: with a plain
    session rollback, the good links collected before the duplicate were
    silently thrown away along with the watermark.
    """
    workspace_id, channel_id = workspace_and_channel
    client = FakeClient(
        [
            FakeMessage(1, "first https://example.com/a.apk"),
            FakeMessage(2, "second https://example.com/b.apk"),
            FakeMessage(3, "repeat https://example.com/a.apk"),
            FakeMessage(4, "fourth https://example.com/c.apk"),
        ]
    )

    collected = _run_collect(client, channel_id)

    assert collected == 3
    assert _stored_urls(workspace_id) == [
        "https://example.com/a.apk",
        "https://example.com/b.apk",
        "https://example.com/c.apk",
    ]


def test_watermark_advances_and_prevents_reprocessing(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    messages = [FakeMessage(7, "one https://example.com/x.apk"), FakeMessage(9, "two https://example.com/y.apk")]

    assert _run_collect(FakeClient(messages), channel_id) == 2

    db = SessionLocal()
    try:
        assert db.get(Channel, channel_id).last_message_id == 9
    finally:
        db.close()

    # A second run over the same history collects nothing new.
    assert _run_collect(FakeClient(messages), channel_id) == 0
    assert len(_stored_urls(workspace_id)) == 2


def test_iterates_oldest_first_so_the_watermark_stays_contiguous(workspace_and_channel):
    """A capped run must not skip past unscanned messages."""
    _, channel_id = workspace_and_channel
    client = FakeClient([FakeMessage(i, f"link https://example.com/{i}.apk") for i in range(1, 6)])

    _run_collect(client, channel_id)

    assert client.iter_kwargs.get("reverse") is True


def test_non_numeric_channel_id_is_skipped_not_fatal():
    """A malformed row must not abort the entire collection run."""
    db = SessionLocal()
    try:
        workspace = Workspace(name="Bad ID WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="not-a-number", username=None)
        db.add(channel)
        db.commit()
        channel_id = channel.id
    finally:
        db.close()

    client = FakeClient([FakeMessage(1, "https://example.com/z.apk")])
    assert _run_collect(client, channel_id) == 0


def test_unresolvable_entity_is_skipped(workspace_and_channel):
    _, channel_id = workspace_and_channel
    client = FakeClient([], entity_error=ValueError("channel not found"))

    assert _run_collect(client, channel_id) == 0


def test_flood_wait_keeps_what_was_already_collected(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    client = FakeClient(
        [FakeMessage(i, f"link https://example.com/{i}.apk") for i in range(1, 6)],
        flood_after=2,
    )

    collected = _run_collect(client, channel_id)

    assert collected == 2
    assert len(_stored_urls(workspace_id)) == 2
    db = SessionLocal()
    try:
        # Watermark reflects only the contiguous progress actually made.
        assert db.get(Channel, channel_id).last_message_id == 2
    finally:
        db.close()


def test_prefers_username_over_numeric_id(workspace_and_channel):
    _, channel_id = workspace_and_channel
    client = FakeClient([])

    _run_collect(client, channel_id)

    assert client.requested_entities == ["testchan"]


def test_message_limit_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("COLLECTOR_MESSAGE_LIMIT", "42")
    assert collector._message_limit() == 42
    monkeypatch.setenv("COLLECTOR_MESSAGE_LIMIT", "not-a-number")
    assert collector._message_limit() == collector.DEFAULT_MESSAGE_LIMIT


def test_collect_stores_the_session_string_encrypted_not_plaintext(monkeypatch):
    """Regression guard for the cleartext-session-string vulnerability.

    A workspace with no active channels lets ``collect()`` exercise its
    real account-provisioning path (env credentials -> encrypted row) and
    return before ever touching the network, so this needs no fake
    Telethon client.
    """
    db = SessionLocal()
    try:
        workspace = Workspace(name="Encryption WS")
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
        db.commit()
    finally:
        db.close()

    raw_session = "raw-plaintext-session-value-that-must-never-hit-the-database-as-is"
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "deadbeefcafebabe")
    monkeypatch.setenv("TG_SESSION_STRING", raw_session)
    monkeypatch.setenv("COLLECTOR_WORKSPACE_ID", str(workspace_id))

    asyncio.run(collector.collect())

    db = SessionLocal()
    try:
        account = db.query(TelegramAccount).filter(TelegramAccount.workspace_id == workspace_id).one()
        assert account.session_string != raw_session
        assert decrypt_field(account.session_string) == raw_session
    finally:
        db.close()
