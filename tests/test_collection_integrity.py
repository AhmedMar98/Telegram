"""Collection integrity: the seven properties a run must never violate.

§44.7 defines collection success as *watermark contiguity*, not as a link
count — "zero links" is a quiet channel read perfectly, and "a thousand
links" can come from a run that failed on half its channels. These tests
pin that definition against the real collector, driven by the same fake
Telethon client the rest of the collector suite uses.

The seventh property is the one that needed new code rather than a new
test: an account that loses ownership of a channel mid-run must not write
that channel's watermark. Nothing stopped it before ``_still_owned_by``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.database import SessionLocal
from app.models import Channel, Link, Message, TelegramAccount, Workspace
from scripts import collect as collector
from tests.test_collector import FakeClient, FakeMessage


@pytest.fixture()
def source() -> tuple[int, int, int]:
    """A workspace, an owning account, and a channel assigned to it."""
    db = SessionLocal()
    try:
        workspace = Workspace(name="Integrity")
        db.add(workspace)
        db.flush()
        account = TelegramAccount(workspace_id=workspace.id, label="owner", session_string="x")
        db.add(account)
        db.flush()
        channel = Channel(
            workspace_id=workspace.id,
            tg_channel_id="-100999",
            username="integrity",
            title="قناة",
            account_id=account.id,
            last_message_id=0,
        )
        db.add(channel)
        db.commit()
        return workspace.id, account.id, channel.id
    finally:
        db.close()


def _collect(client, channel_id: int, *, account_id: int | None = None, is_default: bool = True) -> int:
    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        return asyncio.run(
            collector._collect_channel(
                client, db, channel, None, None, account_id=account_id, is_default=is_default
            )
        )
    finally:
        db.close()


def _watermark(channel_id: int) -> int:
    db = SessionLocal()
    try:
        return db.get(Channel, channel_id).last_message_id
    finally:
        db.close()


def _counts(workspace_id: int) -> tuple[int, int]:
    db = SessionLocal()
    try:
        links = db.query(Link).filter(Link.workspace_id == workspace_id).count()
        messages = db.query(Message).filter(Message.workspace_id == workspace_id).count()
        return links, messages
    finally:
        db.close()


# --- 1. the watermark never goes backwards ---------------------------------


class _OutOfOrderClient(FakeClient):
    """Yields exactly what it was given, in that order, ignoring ``min_id``.

    The standard fake filters by ``min_id`` and sorts ascending, which is
    what Telegram does — and that is precisely why it cannot test this
    property: an older message never reaches the loop, so the watermark
    survives for a reason that has nothing to do with the guard. Written
    only after a sabotage run proved the first version of this test passed
    with ``max()`` removed.
    """

    def iter_messages(self, entity, **kwargs):
        self.iter_kwargs = kwargs
        given = list(self._messages)

        async def _gen():
            for message in given:
                yield message

        return _gen()


def test_the_watermark_never_decreases(source):
    """A high-water mark: it may stall, it may never rewind.

    Sabotage: replace ``max(new_watermark, message.id)`` with
    ``message.id`` and this fails — the watermark follows the last message
    delivered instead of the highest, so one out-of-order delivery rewinds
    it and the messages in between are re-collected forever.
    """
    _, account_id, channel_id = source

    stream = [FakeMessage(50, "https://a.example/high"), FakeMessage(7, "https://a.example/late-old")]
    _collect(_OutOfOrderClient(stream), channel_id, account_id=account_id)

    assert _watermark(channel_id) == 50, "the highest id seen, not the last one delivered"


# --- 2. one message, processed once ----------------------------------------


def test_the_same_message_is_never_ingested_twice(source):
    workspace_id, account_id, channel_id = source
    message = FakeMessage(11, "https://a.example/once")

    _collect(FakeClient([message]), channel_id, account_id=account_id)
    first = _counts(workspace_id)

    # The same message offered again — the overlap the live listener and
    # the scheduled collector have by design.
    _collect(FakeClient([message]), channel_id, account_id=account_id)
    assert _counts(workspace_id) == first


# --- 3. a failed source does not advance its watermark ---------------------


def test_a_source_that_could_not_be_opened_keeps_its_watermark(source):
    _, account_id, channel_id = source
    _collect(FakeClient([FakeMessage(30, "https://a.example/x")]), channel_id, account_id=account_id)
    assert _watermark(channel_id) == 30

    broken = FakeClient([FakeMessage(99, "https://a.example/never")], entity_error=ValueError("no access"))
    assert _collect(broken, channel_id, account_id=account_id) == 0
    assert _watermark(channel_id) == 30, "an unreadable channel must not look collected"


# --- 4. partial failure keeps what succeeded -------------------------------


def test_a_rate_limit_keeps_what_was_already_read(source):
    """FloodWait mid-iteration: everything read before it stays, and the
    watermark stops at the last *contiguous* message so the next run
    resumes there instead of skipping the remainder."""
    workspace_id, account_id, channel_id = source
    messages = [FakeMessage(n, f"https://a.example/{n}") for n in (1, 2, 3, 4, 5)]

    _collect(FakeClient(messages, flood_after=2), channel_id, account_id=account_id)

    links, _ = _counts(workspace_id)
    assert links == 2, "the two messages read before the limit are committed"
    assert _watermark(channel_id) == 2, "and the watermark names exactly them"

    # The next run resumes and loses nothing.
    _collect(FakeClient(messages[2:]), channel_id, account_id=account_id)
    links, _ = _counts(workspace_id)
    assert links == 5
    assert _watermark(channel_id) == 5


# The savepoint property — one duplicate must not discard the links
# collected before it — is guarded by
# ``tests/test_collector.py::test_duplicate_url_does_not_discard_earlier_links``,
# which drives four messages through one run and checks all three
# survivors. A weaker version lived here briefly and was removed: a
# sabotage run showed it still passed with the savepoint taken out, and a
# test that cannot fail for its stated reason is worse than no test,
# because it reads like coverage.


# --- 5. no permanent gap across a reconnect --------------------------------


def test_a_capped_run_leaves_no_permanent_gap(source):
    """A run that stops early must leave the watermark where the *next*
    run will pick up the remainder — the property that makes ascending
    iteration mandatory."""
    workspace_id, account_id, channel_id = source
    everything = [FakeMessage(n, f"https://a.example/m{n}") for n in range(1, 11)]

    _collect(FakeClient(everything[:4]), channel_id, account_id=account_id)
    assert _watermark(channel_id) == 4

    _collect(FakeClient([m for m in everything if m.id > 4]), channel_id, account_id=account_id)

    links, _ = _counts(workspace_id)
    assert links == 10, "every message in the window was collected across the two runs"
    assert _watermark(channel_id) == 10


# --- 6+7. ownership: the old account stops writing --------------------------


def test_an_account_that_lost_the_channel_does_not_move_the_watermark(source):
    """The property that needed code, not just a test.

    An operator presses rebalance while a run is in flight. The old
    account finishes reading and — before this guard — wrote a watermark
    for a channel it no longer owns, putting the new owner's ``min_id``
    past messages nobody had read. A permanent gap, invisible in every
    counter.

    Sabotage: drop the ``_still_owned_by`` check in ``_collect_channel``
    and this fails.
    """
    workspace_id, account_id, channel_id = source

    db = SessionLocal()
    try:
        other = TelegramAccount(workspace_id=workspace_id, label="new owner", session_string="x")
        db.add(other)
        db.flush()
        db.get(Channel, channel_id).account_id = other.id  # reassigned mid-run
        db.commit()
    finally:
        db.close()

    stored = _collect(
        FakeClient([FakeMessage(500, "https://a.example/late")]),
        channel_id,
        account_id=account_id,
        is_default=False,
    )

    assert _watermark(channel_id) == 0, "the new owner's starting point is untouched"
    # What was already read stays read: those links are committed, and the
    # new owner will re-read the window rather than lose it.
    assert stored >= 0


def test_the_owning_account_does_move_the_watermark(source):
    """The counterpart — a guard that refuses everything is also broken."""
    _, account_id, channel_id = source

    _collect(
        FakeClient([FakeMessage(77, "https://a.example/mine")]),
        channel_id,
        account_id=account_id,
        is_default=False,
    )

    assert _watermark(channel_id) == 77


def test_an_unassigned_channel_is_collected_by_the_default_account(source):
    """``account_id IS NULL`` means "inherited by the default account", so
    the ownership guard must not treat it as somebody else's."""
    _, account_id, channel_id = source
    db = SessionLocal()
    try:
        db.get(Channel, channel_id).account_id = None
        db.commit()
    finally:
        db.close()

    _collect(
        FakeClient([FakeMessage(88, "https://a.example/orphan")]),
        channel_id,
        account_id=account_id,
        is_default=True,
    )
    assert _watermark(channel_id) == 88

    # ...and a non-default account must not claim it.
    db = SessionLocal()
    try:
        db.get(Channel, channel_id).last_message_id = 0
        db.commit()
    finally:
        db.close()
    _collect(
        FakeClient([FakeMessage(89, "https://a.example/orphan2")]),
        channel_id,
        account_id=account_id,
        is_default=False,
    )
    assert _watermark(channel_id) == 0


# --- the definition itself: zero is not failure ----------------------------


def test_a_quiet_channel_is_a_successful_collection(source):
    """§44.7's anti-definition, pinned: nothing new is a complete success,
    and it is recorded as having been looked at."""
    _, account_id, channel_id = source
    before = datetime.now(UTC)

    assert _collect(FakeClient([]), channel_id, account_id=account_id) == 0

    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        assert channel.last_message_id == 0
        assert channel.last_collected_at is not None, "it was read, and the row says so"
        assert channel.last_collected_at >= before.replace(tzinfo=None)
    finally:
        db.close()
