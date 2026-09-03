"""The collection runtime, proved without Telegram.

Every rule this phase adds is a property of *this* system — ownership
revalidation, watermark monotonicity, persist-then-advance, backpressure,
recovery, isolation — so every one of them is tested against a fake
reader. That is not a shortcut around real Telegram; it is the reason the
reader interface exists at all. What talks to Telegram
(``app/runtime/telethon_reader.py``) is deliberately thin and is **not**
covered here, and no test in this file should ever be read as evidence
that it works.

Several tests below name their sabotage: the edit that makes them fail.
A guard whose test still passes after the guard is removed is not a test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app import assignments, progress
from app.collection import failures, health
from app.collection import runs as run_log
from app.collection.failures import FailureKind, RetryClass
from app.database import SessionLocal
from app.models import (
    Channel,
    CollectionRun,
    Link,
    Message,
    SourceProgress,
    TelegramAccount,
    Workspace,
)
from app.runtime.metrics import RuntimeMetrics
from app.runtime.protocol import IncomingMessage
from app.runtime.supervisor import RESTART_BACKOFF, Supervisor, SupervisorConfig, restart_delay
from app.runtime.worker import AccountStopped, AccountWorker, WorkerConfig
from app.timeutil import utcnow

# --- fakes ------------------------------------------------------------------


class FakeReader:
    """A ``SourceReader`` that yields what it was given, oldest first.

    Filters by ``min_id`` and sorts ascending because that is what Telegram
    does and what the watermark contract depends on. A fake that ignored
    ``min_id`` would let a broken watermark pass every test in this file.
    """

    def __init__(
        self,
        messages: dict[str, list[IncomingMessage]] | None = None,
        *,
        raise_at: int | None = None,
        error: BaseException | None = None,
        before_yield=None,
    ) -> None:
        self.messages = messages or {}
        self.raise_at = raise_at
        self.error = error
        self.before_yield = before_yield
        self.connected = False
        self.fetches: list[tuple[str, int, int | None]] = []
        self.handler = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def fetch(
        self, source_ref: str, *, min_id: int = 0, max_id: int | None = None, limit: int | None = None
    ) -> AsyncIterator[IncomingMessage]:
        self.fetches.append((source_ref, min_id, limit))
        batch = sorted(
            (
                m
                for m in self.messages.get(source_ref, [])
                if m.message_id > min_id and (max_id is None or m.message_id <= max_id)
            ),
            key=lambda m: m.message_id,
        )
        if limit is not None:
            batch = batch[:limit]
        for index, message in enumerate(batch):
            if self.raise_at is not None and index == self.raise_at:
                raise self.error or RuntimeError("fetch blew up")
            if self.before_yield is not None:
                self.before_yield(source_ref, index)
            yield message
        if self.error is not None and self.raise_at is None:
            raise self.error

    def on_message(self, handler) -> None:
        self.handler = handler


def msg(message_id: int, url: str = "") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        text=url or f"https://example.com/m{message_id}",
        posted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def source():
    """A workspace, an ACTIVE account, and one source assigned to it."""
    with SessionLocal() as db:
        workspace = Workspace(name="runtime")
        db.add(workspace)
        db.flush()
        account = TelegramAccount(workspace_id=workspace.id, label="a", session_string="x")
        db.add(account)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="-100555", username="src", title="قناة")
        db.add(channel)
        db.flush()
        assignments.assign(db, channel, account.id, reason="fixture")
        db.commit()
        return workspace.id, account.id, channel.id


def watermark(channel_id: int, track: str = SourceProgress.LIVE) -> int:
    with SessionLocal() as db:
        row = progress.get(db, channel_id, track)
        return row.current_watermark if row else -1


def mirror(channel_id: int) -> int:
    with SessionLocal() as db:
        return db.get(Channel, channel_id).last_message_id


def link_count(workspace_id: int) -> int:
    with SessionLocal() as db:
        return db.query(Link).filter(Link.workspace_id == workspace_id).count()


def latest_run(channel_id: int) -> CollectionRun | None:
    with SessionLocal() as db:
        return (
            db.query(CollectionRun)
            .filter(CollectionRun.source_id == channel_id)
            .order_by(CollectionRun.id.desc())
            .first()
        )


def build_worker(workspace_id: int, account_id: int, reader, **config) -> AccountWorker:
    return AccountWorker(
        workspace_id=workspace_id,
        account_id=account_id,
        reader=reader,
        session_factory=SessionLocal,
        config=WorkerConfig(**config),
    )


# =========================================================================
# 1. Failure taxonomy and retry policy
# =========================================================================


def test_every_failure_kind_has_a_bounded_policy():
    """Nothing retries forever, and nothing is left without an answer."""
    for kind in FailureKind:
        policy = failures.policy_for(kind)
        assert policy.max_attempts >= 0
        assert policy.max_attempts < 100, f"{kind} retries effectively forever"
        assert policy.delay_for(policy.max_attempts + 1) is None


def test_backoff_never_shrinks_and_the_last_step_repeats():
    policy = failures.policy_for(FailureKind.NETWORK_FAILURE)
    delays = [policy.delay_for(n) for n in range(1, policy.max_attempts + 1)]
    assert all(a <= b for a, b in zip(delays, delays[1:], strict=False))
    assert policy.delay_for(policy.max_attempts) == policy.backoff[-1]


def test_an_unrecognised_error_is_named_unknown_rather_than_guessed():
    """Sabotage: make ``classify`` fall back to NETWORK_FAILURE and this
    fails. A wrong classification applies the wrong policy silently."""

    class SomethingNobodyAnticipated(Exception):
        pass

    assert failures.classify(SomethingNobodyAnticipated()) is FailureKind.UNKNOWN_FAILURE
    assert failures.policy_for(FailureKind.UNKNOWN_FAILURE).max_attempts == 1


def test_classification_walks_the_class_hierarchy():
    """A Telethon subclass must be recognised without being listed."""

    class FloodWaitError(Exception):
        pass

    class SlowerFloodWaitError(FloodWaitError):
        pass

    assert failures.classify(SlowerFloodWaitError()) is FailureKind.RATE_LIMITED


def test_the_wait_telegram_asked_for_wins_over_our_backoff():
    class FloodWaitError(Exception):
        seconds = 900

    assert failures.retry_after(FloodWaitError()) == timedelta(seconds=900)
    assert failures.retry_after(RuntimeError("no number here")) is None


def test_an_assignment_change_is_never_retried():
    """Retrying it is exactly the stale write the ownership rule forbids."""
    policy = failures.policy_for(FailureKind.ASSIGNMENT_CHANGED)
    assert policy.retry_class is RetryClass.POLICY_BLOCKED
    assert policy.max_attempts == 0


# =========================================================================
# 2. What a run may claim
# =========================================================================


def test_a_run_that_never_examined_its_scope_is_a_failure_not_a_success(source):
    """The phase's hardest prohibition, in one test.

    "The connection succeeded, so the run succeeded" is the claim this
    refuses. Sabotage: drop the ``scope_examined`` branch in
    ``runs.complete`` and this fails.
    """
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        run = run_log.start(db, channel, mode=SourceProgress.LIVE, account_id=account_id)
        run_log.complete(db, run, messages_seen=0, links_stored=0, watermark_after=0, scope_examined=False)
        db.commit()
        assert run.state == CollectionRun.FAILED
        assert run.failure_kind == FailureKind.UNKNOWN_FAILURE.value


def test_a_run_that_found_nothing_new_still_completes(source):
    """Zero is a real answer. A quiet channel read perfectly is success."""
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        run = run_log.start(db, channel, mode=SourceProgress.LIVE, account_id=account_id)
        run_log.complete(db, run, messages_seen=0, links_stored=0, watermark_after=0, scope_examined=True)
        db.commit()
        assert run.state == CollectionRun.COMPLETED


def test_a_failed_run_keeps_what_it_had_already_stored(source):
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        run = run_log.start(db, channel, mode=SourceProgress.LIVE, account_id=account_id)
        run_log.fail(db, run, kind=FailureKind.NETWORK_FAILURE, detail="reset", messages_seen=7, links_stored=3)
        db.commit()
        assert (run.state, run.messages_seen, run.links_stored) == (CollectionRun.FAILED, 7, 3)
        assert run.heartbeat_at is None


def test_recovery_closes_runs_a_dead_worker_left_open_and_leaves_the_watermark(source):
    """Sabotage: have ``recover_abandoned`` reset the watermark and this
    fails — the resume point *is* the persisted data's edge."""
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        progress.advance(db, channel, 120, account_id=account_id)
        run = run_log.start(db, channel, mode=SourceProgress.LIVE, account_id=account_id)
        run.heartbeat_at = utcnow() - timedelta(hours=1)
        db.commit()
        run_id = run.id

    with SessionLocal() as db:
        recovered = run_log.recover_abandoned(db, workspace_id)
        db.commit()
        assert [r.id for r in recovered] == [run_id]
        assert recovered[0].failure_kind == FailureKind.WORKER_FAILURE.value

    assert watermark(channel_id) == 120


def test_recovery_leaves_a_run_whose_worker_is_still_beating(source):
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        run = run_log.start(db, db.get(Channel, channel_id), mode=SourceProgress.LIVE, account_id=account_id)
        db.commit()
        run_id = run.id
    with SessionLocal() as db:
        assert run_log.recover_abandoned(db, workspace_id) == []
        assert db.get(CollectionRun, run_id).state == CollectionRun.RUNNING


# =========================================================================
# 3. Watermark invariants
# =========================================================================


def test_progress_starts_at_the_legacy_watermark_not_at_zero(source):
    """Starting at zero would re-read the whole archive and call it new."""
    _, _, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        channel.last_message_id = 4200
        db.commit()
        assert progress.ensure(db, channel, SourceProgress.LIVE).current_watermark == 4200
        assert progress.ensure(db, channel, SourceProgress.HISTORICAL).current_watermark == 0


def test_the_watermark_refuses_to_move_backwards(source):
    """Sabotage: delete the ``watermark < current`` branch in
    ``progress.advance`` and this fails."""
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        progress.advance(db, channel, 90, account_id=account_id)
        result = progress.advance(db, channel, 40, account_id=account_id)
        db.commit()
    assert result.refused == progress.WOULD_REGRESS
    assert result.advanced is False
    assert watermark(channel_id) == 90


def test_an_account_that_lost_the_assignment_cannot_write_progress(source):
    """The ownership invariant, and the one that needed code.

    Sabotage: remove the ``current_account_id`` check in
    ``progress.advance`` and this fails — the departing worker moves the
    new owner's resume point past messages nobody has read.
    """
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        other = TelegramAccount(workspace_id=workspace_id, label="b", session_string="x")
        db.add(other)
        db.flush()
        assignments.assign(db, db.get(Channel, channel_id), other.id, reason="rebalance")
        db.commit()

    with SessionLocal() as db:
        result = progress.advance(db, db.get(Channel, channel_id), 500, account_id=account_id)
        db.commit()

    assert result.refused == progress.STALE_ASSIGNMENT
    assert watermark(channel_id) == 0, "the new owner's starting point is untouched"


def test_the_owning_account_does_move_it(source):
    """A guard that refuses everything is also broken."""
    _, account_id, channel_id = source
    with SessionLocal() as db:
        result = progress.advance(db, db.get(Channel, channel_id), 77, account_id=account_id)
        db.commit()
    assert result.advanced and watermark(channel_id) == 77
    assert mirror(channel_id) == 77, "the legacy column mirrors the live track"


def test_re_writing_the_same_watermark_is_not_progress(source):
    """ "Nothing new" must be distinguishable from "moved forward"."""
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        progress.advance(db, channel, 50, account_id=account_id)
        db.commit()
        first = progress.get(db, channel_id).last_progress_at

    with SessionLocal() as db:
        result = progress.advance(db, db.get(Channel, channel_id), 50, account_id=account_id)
        db.commit()
        assert result.advanced is False and result.refused is None
        assert progress.get(db, channel_id).last_progress_at == first


def test_live_and_historical_tracks_do_not_collide(source):
    """One scalar cannot carry two readers — this is why there are two rows.

    Sabotage: drop ``track`` from the uniqueness of a progress row and the
    backfill's cursor lands in the live watermark, which is the exact
    corruption the model exists to prevent.
    """
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        progress.advance(db, channel, 900, account_id=account_id, track=SourceProgress.LIVE)
        progress.advance(db, channel, 30, account_id=account_id, track=SourceProgress.HISTORICAL)
        db.commit()

    assert watermark(channel_id, SourceProgress.LIVE) == 900
    assert watermark(channel_id, SourceProgress.HISTORICAL) == 30
    assert mirror(channel_id) == 900, "only the live track mirrors"


def test_an_attempt_is_recorded_separately_from_progress(source):
    """ "Tried recently and got nowhere" must be a state you can see."""
    _, _, channel_id = source
    with SessionLocal() as db:
        progress.record_attempt(db, db.get(Channel, channel_id))
        db.commit()
        row = progress.get(db, channel_id)
        assert row.last_attempt_at is not None
        assert row.last_progress_at is None


def test_coverage_defaults_to_unknown_rather_than_clean(source):
    """ "No gap detected" is a claim; the honest default is "we cannot say"."""
    _, _, channel_id = source
    with SessionLocal() as db:
        row = progress.ensure(db, db.get(Channel, channel_id))
        db.commit()
        assert row.coverage_status == SourceProgress.UNKNOWN_COVERAGE


# =========================================================================
# 4. The worker
# =========================================================================


def test_a_cycle_stores_links_and_advances_the_watermark(source):
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1), msg(2), msg(3)]})
    worker = build_worker(workspace_id, account_id, reader)

    report = asyncio.run(worker.cycle())

    assert report.sources_considered == 1
    assert report.runs_completed == 1
    assert report.messages_seen == 3
    assert watermark(channel_id) == 3
    assert link_count(workspace_id) == 3
    assert latest_run(channel_id).state == CollectionRun.COMPLETED


def test_a_second_cycle_resumes_from_the_watermark(source):
    """The fetch must ask for ``min_id`` at the watermark, not at zero."""
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1), msg(2)]})
    worker = build_worker(workspace_id, account_id, reader)

    asyncio.run(worker.cycle())
    asyncio.run(worker.cycle())

    assert [f[1] for f in reader.fetches] == [0, 2]
    assert watermark(channel_id) == 2


def test_a_quiet_source_completes_without_moving_anything(source):
    workspace_id, account_id, channel_id = source
    worker = build_worker(workspace_id, account_id, FakeReader({"src": []}))

    report = asyncio.run(worker.cycle())

    assert report.runs_completed == 1
    assert report.watermarks_advanced == 0
    assert watermark(channel_id) == 0
    assert latest_run(channel_id).state == CollectionRun.COMPLETED


def test_a_source_the_account_no_longer_holds_costs_no_telegram_traffic(source):
    """Revalidation before the fetch, not only before the write."""
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        other = TelegramAccount(workspace_id=workspace_id, label="b", session_string="x")
        db.add(other)
        db.flush()
        assignments.assign(db, db.get(Channel, channel_id), other.id, reason="moved")
        db.commit()

    reader = FakeReader({"src": [msg(1)]})
    worker = build_worker(workspace_id, account_id, reader)
    report = asyncio.run(worker.cycle())

    assert report.sources_considered == 0, "the source is not in this account's list any more"
    assert reader.fetches == []


def test_an_assignment_that_moves_mid_run_keeps_the_links_and_drops_the_watermark(source):
    """The race this runtime is built around.

    An operator rebalances while the fetch is in flight. Everything read is
    already committed and stays; the watermark belongs to the new owner.

    Sabotage: remove the ownership re-check inside ``progress.advance`` and
    this fails on the watermark assertion.
    """
    workspace_id, account_id, channel_id = source

    def steal(_ref, index):
        if index != 1:
            return
        with SessionLocal() as db:
            other = TelegramAccount(workspace_id=workspace_id, label="b", session_string="x")
            db.add(other)
            db.flush()
            assignments.assign(db, db.get(Channel, channel_id), other.id, reason="rebalance")
            db.commit()

    reader = FakeReader({"src": [msg(1), msg(2), msg(3)]}, before_yield=steal)
    worker = build_worker(workspace_id, account_id, reader, flush_every=1)
    report = asyncio.run(worker.cycle())

    assert watermark(channel_id) == 0, "the new owner resumes from where it always was"
    assert link_count(workspace_id) > 0, "what was read is stored, and is not re-collected"
    assert report.runs_failed == 1
    assert latest_run(channel_id).failure_kind == FailureKind.ASSIGNMENT_CHANGED.value


def test_a_fetch_that_blows_up_leaves_the_watermark_where_the_next_run_resumes(source):
    """Persist-then-advance, from the failure side.

    Sabotage: advance the watermark before the chunk is committed and this
    still passes — but ``test_a_partial_read_is_not_a_completed_run``
    below stops recording it as a success, and the two together are what
    make a crash cost a re-read instead of a gap.
    """
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1), msg(2), msg(3), msg(4)]}, raise_at=2, error=ConnectionError("reset"))
    worker = build_worker(workspace_id, account_id, reader, flush_every=1)

    report = asyncio.run(worker.cycle())

    assert report.runs_failed == 1
    assert watermark(channel_id) == 0
    assert link_count(workspace_id) == 2, "the two messages read before the failure are stored"
    run = latest_run(channel_id)
    assert run.state == CollectionRun.FAILED
    assert run.failure_kind == FailureKind.NETWORK_FAILURE.value


def test_a_partial_read_is_not_a_completed_run(source):
    """A run that did not finish its range may not be recorded as one."""
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1), msg(2)]}, raise_at=1, error=TimeoutError("slow"))
    worker = build_worker(workspace_id, account_id, reader)

    asyncio.run(worker.cycle())

    run = latest_run(channel_id)
    assert run.state == CollectionRun.FAILED
    assert run.failure_kind == FailureKind.TIMEOUT.value


def test_a_failure_defers_the_source_with_growing_backoff(source):
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1)]}, error=ConnectionError("down"))
    worker = build_worker(workspace_id, account_id, reader)

    asyncio.run(worker.cycle())
    first = worker.deferral_for(channel_id)
    assert first is not None and first.attempts == 1

    # The next cycle must not touch it until the backoff has elapsed.
    report = asyncio.run(worker.cycle())
    assert report.sources_deferred == 1

    later = utcnow() + timedelta(hours=2)
    asyncio.run(worker.cycle(now=later))
    assert worker.deferral_for(channel_id).attempts == 2


def test_a_source_out_of_attempts_stops_rather_than_hammering(source):
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1)]}, error=ConnectionError("down"))
    worker = build_worker(workspace_id, account_id, reader)

    moment = utcnow()
    policy = failures.policy_for(FailureKind.NETWORK_FAILURE)
    for _ in range(policy.max_attempts + 1):
        moment += timedelta(days=1)
        asyncio.run(worker.cycle(now=moment))

    deferral = worker.deferral_for(channel_id)
    assert deferral is not None and deferral.until is None


def test_an_auth_failure_stops_the_account_rather_than_the_source(source):
    """A revoked session does not get better by trying the next channel."""
    workspace_id, account_id, _ = source

    class AuthKeyUnregisteredError(Exception):
        pass

    reader = FakeReader({"src": [msg(1)]}, error=AuthKeyUnregisteredError("revoked"))
    worker = build_worker(workspace_id, account_id, reader)

    with pytest.raises(AccountStopped) as caught:
        asyncio.run(worker.cycle())
    assert caught.value.kind is FailureKind.AUTH_FAILURE


def test_a_rate_limit_pauses_the_whole_account_for_the_wait_telegram_named(source):
    """Per-account, because Telegram's limits are per account."""
    workspace_id, account_id, _ = source

    class FloodWaitError(Exception):
        seconds = 3600

    reader = FakeReader({"src": [msg(1)]}, error=FloodWaitError("slow down"))
    worker = build_worker(workspace_id, account_id, reader)

    asyncio.run(worker.cycle())
    assert worker.metrics.runs_failed[FailureKind.RATE_LIMITED.value] == 1

    report = asyncio.run(worker.cycle())
    assert report.sources_considered == 0, "paused: the cycle does nothing at all"
    assert worker.metrics.rate_limit_pauses == 1

    asyncio.run(worker.cycle(now=utcnow() + timedelta(hours=2)))
    assert worker.metrics.rate_limit_pauses == 1, "the pause expires"


def test_a_full_batch_refuses_to_claim_coverage(source):
    """A capped run says nothing about what it did not read."""
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(n) for n in range(1, 6)]})
    worker = build_worker(workspace_id, account_id, reader, batch_limit=2, flush_every=1)

    asyncio.run(worker.cycle())
    with SessionLocal() as db:
        assert progress.get(db, channel_id).coverage_status == SourceProgress.UNKNOWN_COVERAGE
    assert watermark(channel_id) == 2

    # Reading to the end of what exists is the only thing that lets it say
    # the examined range was contiguous.
    for _ in range(3):
        asyncio.run(worker.cycle())
    with SessionLocal() as db:
        assert progress.get(db, channel_id).coverage_status == SourceProgress.NO_DETECTED_GAP


def test_a_capped_run_leaves_no_gap_across_cycles(source):
    """Contiguity is the definition of collection success, not a link count."""
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(n) for n in range(1, 11)]})
    worker = build_worker(workspace_id, account_id, reader, batch_limit=3, flush_every=1)

    for _ in range(5):
        asyncio.run(worker.cycle())

    assert watermark(channel_id) == 10
    with SessionLocal() as db:
        seen = {row.tg_message_id for row in db.query(Message).filter(Message.channel_id == channel_id).all()}
    assert seen == set(range(1, 11)), "every message in the window was examined"


# =========================================================================
# 5. The live path and backpressure
# =========================================================================


def test_the_live_path_stores_without_advancing_the_watermark(source):
    """Two readers, one authority. Sabotage: have the drain call
    ``progress.advance`` and this fails — and a dropped message then
    becomes a permanent hole."""
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": []})
    worker = build_worker(workspace_id, account_id, reader)

    async def scenario():
        await worker.cycle()  # builds the routing table
        reader.handler("-100555", msg(4321))
        drain = asyncio.create_task(worker.drain_live())
        await worker._queue.join()
        drain.cancel()

    asyncio.run(scenario())

    assert link_count(workspace_id) == 1, "the message is stored at once"
    assert watermark(channel_id) == 0, "and the watermark did not move"


def test_a_full_live_queue_drops_and_the_sweep_picks_it_up_anyway(source):
    """Backpressure costs latency, not data — because of the rule above.

    Sabotage: let the live path advance the watermark, and the dropped
    message below is skipped by the sweep too. That is the failure this
    design is shaped to make impossible.
    """
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1), msg(2)]})
    worker = build_worker(workspace_id, account_id, reader, queue_size=1)

    worker._on_live_message("-100555", msg(1))
    worker._on_live_message("-100555", msg(2))  # queue is full

    assert worker.metrics.live_dropped == 1
    assert worker.metrics.live_delivered == 2

    asyncio.run(worker.cycle())
    with SessionLocal() as db:
        seen = {r.tg_message_id for r in db.query(Message).filter(Message.channel_id == channel_id)}
    assert seen == {1, 2}, "the dropped message was read by the sweep"


def test_a_live_message_for_an_unassigned_source_is_counted_not_stored(source):
    workspace_id, account_id, _ = source
    worker = build_worker(workspace_id, account_id, FakeReader({"src": []}))

    async def scenario():
        await worker.cycle()
        worker._on_live_message("-100999999", msg(5))
        drain = asyncio.create_task(worker.drain_live())
        await worker._queue.join()
        drain.cancel()

    asyncio.run(scenario())
    assert worker.metrics.live_unassigned == 1
    assert link_count(workspace_id) == 0


def test_a_live_message_routes_by_id_or_username(source):
    workspace_id, account_id, channel_id = source
    worker = build_worker(workspace_id, account_id, FakeReader({"src": []}))
    asyncio.run(worker.cycle())

    assert worker.route("-100555") == channel_id
    assert worker.route("555") == channel_id, "the canonical form Telethon reports"
    assert worker.route("@SRC") == channel_id
    assert worker.route("-100000") is None


# =========================================================================
# 6. Lifecycle
# =========================================================================


def test_a_heartbeat_slower_than_the_recovery_window_is_refused():
    """A healthy run must never look abandoned. Sabotage: delete the
    ``__post_init__`` check and a misconfigured deployment silently has
    every run reaped mid-flight."""
    with pytest.raises(ValueError, match="ABANDONED_AFTER"):
        WorkerConfig(heartbeat_interval=run_log.ABANDONED_AFTER.total_seconds() + 1)


def test_run_connects_sweeps_and_disconnects_on_stop(source):
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(1)]})
    worker = build_worker(workspace_id, account_id, reader, cycle_pause=0.01)

    async def scenario():
        task = asyncio.create_task(worker.run())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if watermark(channel_id) == 1:
                break
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())

    assert watermark(channel_id) == 1
    assert reader.connected is False, "shutdown always disconnects"
    assert worker.metrics.reader_connects == 1


def test_a_shutdown_mid_range_cancels_the_run_rather_than_completing_it(source):
    """Stopped on purpose is not the same fact as finished, or as failed.

    Sabotage: drop the ``stopped_early`` branch in ``_finish_run`` and the
    interrupted run is recorded COMPLETED — a claim that the whole range
    was examined, which is the exact overstatement this phase forbids.
    """
    workspace_id, account_id, channel_id = source

    worker = None

    def stop_after_first(_ref, index):
        if index == 1:
            worker.stop()

    reader = FakeReader({"src": [msg(n) for n in range(1, 8)]}, before_yield=stop_after_first)
    worker = build_worker(workspace_id, account_id, reader, flush_every=1)

    asyncio.run(worker.cycle())

    run = latest_run(channel_id)
    assert run.state == CollectionRun.CANCELLED
    assert watermark(channel_id) == 1, "what was read is kept; the rest is the next run's"
    with SessionLocal() as db:
        assert progress.get(db, channel_id).coverage_status == SourceProgress.UNKNOWN_COVERAGE


def test_shutdown_drains_what_is_already_queued(source):
    workspace_id, account_id, _ = source
    reader = FakeReader({"src": []})
    worker = build_worker(workspace_id, account_id, reader, cycle_pause=0.01)

    async def scenario():
        task = asyncio.create_task(worker.run())
        # Wait for the routing table rather than for a duration: a live
        # message that arrives before the first cycle has nowhere to go,
        # and a fixed sleep makes that a coin flip on a loaded machine.
        for _ in range(500):
            await asyncio.sleep(0.01)
            if worker.route("-100555") is not None:
                break
        worker._on_live_message("-100555", msg(9))
        worker.stop()
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert link_count(workspace_id) == 1


# =========================================================================
# 7. The supervisor
# =========================================================================


def test_restart_backoff_is_bounded_and_never_a_tight_loop():
    delays = [restart_delay(n) for n in range(1, 10)]
    assert delays[0] >= timedelta(seconds=1)
    assert all(a <= b for a, b in zip(delays, delays[1:], strict=False))
    assert delays[-1] == RESTART_BACKOFF[-1]


def _supervisor(workspace_id: int, readers: dict[int, FakeReader], **config) -> Supervisor:
    return Supervisor(
        workspace_id=workspace_id,
        session_factory=SessionLocal,
        reader_factory=lambda account: readers[account.id],
        config=SupervisorConfig(**config),
    )


def test_startup_recovery_runs_before_any_worker(source):
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        run = run_log.start(db, db.get(Channel, channel_id), mode=SourceProgress.LIVE, account_id=account_id)
        run.heartbeat_at = utcnow() - timedelta(hours=2)
        db.commit()
        run_id = run.id

    supervisor = _supervisor(workspace_id, {account_id: FakeReader()})
    assert supervisor.recover() == [run_id]
    with SessionLocal() as db:
        assert db.get(CollectionRun, run_id).state == CollectionRun.FAILED


def test_only_active_accounts_get_a_worker(source):
    workspace_id, account_id, _ = source
    with SessionLocal() as db:
        from app import accounts as account_service

        blocked = TelegramAccount(workspace_id=workspace_id, label="b", session_string="x")
        db.add(blocked)
        db.flush()
        account_service.set_state(db, blocked, TelegramAccount.AUTH_REQUIRED, reason="revoked")
        db.commit()

    supervisor = _supervisor(workspace_id, {account_id: FakeReader()})
    assert [a.id for a in supervisor.eligible_accounts()] == [account_id]


def test_a_worker_that_dies_is_restarted_but_a_retired_account_is_not(source):
    """The distinction that stops a revoked session being retried forever."""
    workspace_id, account_id, _ = source
    supervisor = _supervisor(workspace_id, {account_id: FakeReader()})

    async def scenario():
        await supervisor.supervise_once()
        slot = supervisor._slots[account_id]
        assert slot.task is not None

        # A crash: restartable, with a delay.
        slot.task.cancel()
        task: asyncio.Task = asyncio.ensure_future(_boom())
        with pytest.raises(RuntimeError):
            await task
        slot.task = task
        await supervisor.supervise_once()
        assert supervisor._slots[account_id].consecutive_failures == 1
        assert supervisor._slots[account_id].restart_at > 0

        # An account failure: retired, never restarted.
        stopped: asyncio.Task = asyncio.ensure_future(_stopped())
        with pytest.raises(AccountStopped):
            await stopped
        supervisor._slots[account_id].task = stopped
        await supervisor.supervise_once()
        assert supervisor._slots[account_id].retired_reason is not None
        assert supervisor._slots[account_id].task is None
        await supervisor.shutdown()

    async def _boom():
        raise RuntimeError("worker died")

    async def _stopped():
        raise AccountStopped(FailureKind.AUTH_FAILURE, "revoked")

    asyncio.run(scenario())


# =========================================================================
# 8. Health: is collection actually working?
# =========================================================================


def test_a_source_nobody_has_attempted_is_reported_as_stalled(source):
    workspace_id, _, channel_id = source
    with SessionLocal() as db:
        findings = health.stalled(db, workspace_id)
    assert [(f.source_id, f.kind) for f in findings] == [(channel_id, health.Finding.STALLED)]


def test_a_source_attempted_recently_is_not_stalled(source):
    workspace_id, _, channel_id = source
    with SessionLocal() as db:
        progress.record_attempt(db, db.get(Channel, channel_id))
        db.commit()
        assert health.stalled(db, workspace_id) == []


def test_attempting_forever_without_moving_is_a_silent_failure(source):
    """The failure this phase exists to catch: every counter alive, and
    the watermark has not moved in a week."""
    workspace_id, _, channel_id = source
    with SessionLocal() as db:
        row = progress.ensure(db, db.get(Channel, channel_id))
        row.created_at = utcnow() - timedelta(days=30)
        row.last_attempt_at = utcnow()
        db.commit()
        findings = health.silent_failures(db, workspace_id)
    assert [(f.source_id, f.kind) for f in findings] == [(channel_id, health.Finding.SILENT_FAILURE)]


def test_a_source_that_has_only_just_appeared_is_not_a_silent_failure(source):
    workspace_id, _, channel_id = source
    with SessionLocal() as db:
        progress.record_attempt(db, db.get(Channel, channel_id))
        db.commit()
        assert health.silent_failures(db, workspace_id) == []


def test_a_watermark_with_no_examined_message_below_it_is_a_coverage_gap(source):
    """Progress claiming a position no stored evidence supports."""
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        progress.advance(db, db.get(Channel, channel_id), 5000, account_id=account_id)
        db.commit()
        findings = health.coverage_gaps(db, workspace_id)
    assert [(f.source_id, f.kind) for f in findings] == [(channel_id, health.Finding.COVERAGE_GAP)]


def test_a_watermark_backed_by_examined_messages_is_not_a_gap(source):
    workspace_id, account_id, channel_id = source
    worker = build_worker(workspace_id, account_id, FakeReader({"src": [msg(1), msg(2)]}))
    asyncio.run(worker.cycle())
    with SessionLocal() as db:
        assert health.coverage_gaps(db, workspace_id) == []


def test_a_hung_worker_is_visible_while_the_process_is_still_running(source):
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        run = run_log.start(db, db.get(Channel, channel_id), mode=SourceProgress.LIVE, account_id=account_id)
        run.heartbeat_at = utcnow() - timedelta(hours=3)
        db.commit()
        findings = health.abandoned_runs(db, workspace_id)
    assert [f.kind for f in findings] == [health.Finding.ABANDONED_RUN]


# =========================================================================
# 9. The metrics contract
# =========================================================================


def test_there_is_no_single_success_rate_to_read():
    """The prohibition, enforced by there being no field for it.

    One ratio averages four different failures into a number that looks
    healthy in all of them. Sabotage: add ``success_rate`` to
    ``RuntimeMetrics`` and this fails.
    """
    fields = set(vars(RuntimeMetrics()))
    for banned in ("success_rate", "system_success_rate", "health_score", "uptime_percent"):
        assert banned not in fields
    for required in ("runs_started", "runs_completed", "runs_failed", "progress_advanced"):
        assert required in fields


def test_completed_and_advanced_are_counted_separately(source):
    """ "We looked and there was nothing new" must stay distinguishable
    from "we looked and something went wrong"."""
    workspace_id, account_id, _ = source
    worker = build_worker(workspace_id, account_id, FakeReader({"src": [msg(1)]}))

    asyncio.run(worker.cycle())
    asyncio.run(worker.cycle())

    assert worker.metrics.runs_completed == 2
    assert worker.metrics.progress_advanced == 1


def test_a_supervisor_folds_worker_counters_into_its_own():
    total = RuntimeMetrics()
    one = RuntimeMetrics(runs_completed=2, messages_seen=10)
    one.failure(FailureKind.TIMEOUT.value)
    total.merge(one)
    total.merge(one)
    assert total.runs_completed == 4
    assert total.snapshot()["runs_failed"] == {FailureKind.TIMEOUT.value: 2}


# =========================================================================
# 10. Historical collection
# =========================================================================


def test_a_backfill_walks_its_window_and_resumes_where_it_stopped(source):
    """From/To, bounded, and re-entrant.

    Sabotage: have ``_open_run`` ignore the window's ``from_id`` and the
    first call starts at zero, re-reading everything below the window.
    """
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(n) for n in range(1, 21)]})
    worker = build_worker(workspace_id, account_id, reader, batch_limit=3, flush_every=1)

    first = asyncio.run(worker.backfill(channel_id, from_id=5, to_id=12))
    assert first.kind is None
    assert reader.fetches[-1][1] == 5, "started at the window, not at zero"
    assert watermark(channel_id, SourceProgress.HISTORICAL) == 8

    asyncio.run(worker.backfill(channel_id, from_id=5, to_id=12))
    assert reader.fetches[-1][1] == 8, "resumed where the first call stopped"

    for _ in range(4):
        asyncio.run(worker.backfill(channel_id, from_id=5, to_id=12))

    assert watermark(channel_id, SourceProgress.HISTORICAL) == 12, "stopped at the window's end"
    with SessionLocal() as db:
        seen = {row.tg_message_id for row in db.query(Message).filter(Message.channel_id == channel_id).all()}
    assert seen == set(range(6, 13)), "exactly the requested window, nothing outside it"


def test_a_backfill_never_touches_the_live_watermark(source):
    """The corruption the track column exists to prevent.

    Sabotage: drop ``track`` from ``_run_source`` so the backfill writes
    the LIVE row, and the live reader resumes from the backfill's cursor —
    skipping every message in between, permanently.
    """
    workspace_id, account_id, channel_id = source
    reader = FakeReader({"src": [msg(n) for n in range(1, 31)]})
    worker = build_worker(workspace_id, account_id, reader, flush_every=1)

    # Live gets to the head of the channel first.
    asyncio.run(worker.cycle())
    assert watermark(channel_id, SourceProgress.LIVE) == 30

    # Then a backfill is asked for an early window.
    asyncio.run(worker.backfill(channel_id, from_id=2, to_id=6))

    assert watermark(channel_id, SourceProgress.LIVE) == 30, "the live frontier is untouched"
    assert watermark(channel_id, SourceProgress.HISTORICAL) == 6
    assert mirror(channel_id) == 30, "and so is the legacy mirror"


def test_a_historical_run_records_the_window_a_person_asked_for(source):
    """Ids are what Telegram takes; dates are what a person means."""
    workspace_id, account_id, channel_id = source
    worker = build_worker(workspace_id, account_id, FakeReader({"src": [msg(3)]}))

    asked_from = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    asked_to = datetime(2026, 2, 1, tzinfo=UTC).replace(tzinfo=None)
    asyncio.run(worker.backfill(channel_id, from_id=1, to_id=9, range_from=asked_from, range_to=asked_to))

    run = latest_run(channel_id)
    assert run.mode == SourceProgress.HISTORICAL
    assert (run.range_from, run.range_to) == (asked_from, asked_to)
    assert run.state == CollectionRun.COMPLETED


def test_a_backfill_that_lost_the_assignment_writes_nothing(source):
    """Ownership is revalidated on the historical track too."""
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        other = TelegramAccount(workspace_id=workspace_id, label="b", session_string="x")
        db.add(other)
        db.flush()
        assignments.assign(db, db.get(Channel, channel_id), other.id, reason="moved")
        db.commit()

    worker = build_worker(workspace_id, account_id, FakeReader({"src": [msg(2)]}))
    outcome = asyncio.run(worker.backfill(channel_id, from_id=1, to_id=5))

    assert outcome.kind is FailureKind.ASSIGNMENT_CHANGED
    assert watermark(channel_id, SourceProgress.HISTORICAL) == -1, "no progress row was even created"


# =========================================================================
# 11. Live lag is measured, not asserted
# =========================================================================


def test_live_lag_is_measured_from_posting_to_storage(source):
    workspace_id, account_id, _ = source
    reader = FakeReader({"src": []})
    worker = build_worker(workspace_id, account_id, reader)

    async def scenario():
        await worker.cycle()
        posted = utcnow() - timedelta(seconds=30)
        reader.handler(
            "-100555",
            IncomingMessage(message_id=1, text="https://e.example/x", posted_at=posted),
        )
        drain = asyncio.create_task(worker.drain_live())
        await worker._queue.join()
        drain.cancel()

    asyncio.run(scenario())

    assert worker.metrics.live_lag_samples == 1
    assert 29 <= worker.metrics.live_lag_mean_seconds <= 120
    assert worker.metrics.live_lag_max_seconds == worker.metrics.live_lag_mean_seconds


def test_lag_with_no_samples_is_unknown_rather_than_zero():
    """Zero would report "instant" where the truth is "never measured"."""
    assert RuntimeMetrics().live_lag_mean_seconds is None


def test_merging_lag_adds_totals_and_keeps_the_worst_maximum():
    total = RuntimeMetrics()
    slow = RuntimeMetrics()
    slow.observe_live_lag(10.0)
    fast = RuntimeMetrics()
    fast.observe_live_lag(2.0)
    total.merge(slow)
    total.merge(fast)
    assert total.live_lag_samples == 2
    assert total.live_lag_mean_seconds == 6.0
    assert total.live_lag_max_seconds == 10.0
