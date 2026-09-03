"""One account, doing the collecting. The data plane.

Separation of planes, concretely
--------------------------------
The **control plane** decides who should collect what: ``app.access``,
``app.eligibility``, ``app.assignment``, ``app.assignments``. It runs in
the web process, in the dashboard, in a rebalance — anywhere, at any
time, without a Telegram connection.

The **data plane** is this file. It holds one Telegram connection, reads
what the control plane assigned to it, and writes messages, links and
progress. It decides nothing about ownership; it only ever asks.

The consequence that matters: an operator pressing "rebalance" commits a
control-plane change while a worker is mid-fetch. The worker is not
notified — it re-reads the assignment before writing anything, every
time. That is the whole coordination protocol, and it is deliberately
that small, because a fleet whose members must be *told* things is a
fleet that loses messages whenever a message is missed.

Two readers, one authority
--------------------------
There are two paths into storage and they overlap on purpose:

- the **live path** — Telegram pushes a message, it is ingested at once;
- the **sweep** — a run reads from the watermark forward, oldest first.

Only the sweep moves the watermark. The live path stores and stops. That
is what makes backpressure safe: when the live queue is full a message is
*dropped*, and dropping it costs latency and nothing else, because the
watermark never passed it and the next sweep reads it again. Had the live
path advanced the watermark, one dropped message would be a permanent
hole nobody would ever notice.

The overlap is already how ``app.live`` and ``scripts/collect`` coexist:
``ingest_text`` refuses a message id already in ``messages``, so the
second reader to arrive stores nothing and says so.

Where each half runs
--------------------
Telegram is async and SQLAlchemy here is not. The fetch therefore stays
on the event loop — a Telethon client belongs to the loop it was created
on and cannot be driven from a worker thread — and every database
statement goes through ``asyncio.to_thread``. Messages are carried
between the two halves in bounded chunks, so neither side has to hold a
whole channel in memory.

Persist, then advance
---------------------
Ingested rows are committed before the watermark moves, and the watermark
moves in a transaction of its own together with the run that produced it.
A crash between the two costs a re-read. A crash the other way round
costs the messages in between, permanently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import assignments, progress
from app.collection import failures as failure_taxonomy
from app.collection import runs as run_log
from app.collection.failures import FailureKind
from app.identity import canonical_id
from app.ingest import ingest_text
from app.models import Channel, CollectionRun, SourceAssignment, SourceProgress
from app.rls import scope_session_to_workspace
from app.runtime.metrics import RuntimeMetrics
from app.runtime.protocol import IncomingMessage, SourceReader
from app.timeutil import utcnow

logger = logging.getLogger(__name__)


class AccountStopped(RuntimeError):
    """The account itself cannot work, so neither can this worker.

    Distinct from a source failing: a revoked session does not get better
    by trying the next channel, and continuing would burn the whole
    assignment list against the same error.
    """

    def __init__(self, kind: FailureKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class WorkerConfig:
    """Knobs, with the reasoning for each number.

    **None of these is a measured capacity.** They are bounds chosen so
    that one misbehaving source cannot consume a cycle, and they are
    parameters precisely so a deployment that measures something better
    can say so rather than inherit a guess.
    """

    #: Messages read per source per run. Bounds one very active channel;
    #: the watermark stays contiguous, so the next run resumes exactly
    #: where this one stopped.
    batch_limit: int = 200
    #: Messages held between the fetch and the storage thread before the
    #: chunk is written. Small enough that a crash loses little work,
    #: large enough that the thread hop is not paid per message.
    flush_every: int = 50
    #: Live messages buffered before dropping. Dropping is latency, not
    #: loss — see the module docstring.
    queue_size: int = 500
    #: Pause between sweeps of the whole assignment list.
    cycle_pause: float = 30.0
    #: How often a long fetch says it is still alive. Must stay well below
    #: ``runs.ABANDONED_AFTER``, or a healthy run looks abandoned.
    heartbeat_interval: float = 30.0
    #: How long shutdown waits for the live queue to finish draining.
    shutdown_grace: float = 10.0

    def __post_init__(self) -> None:
        if self.heartbeat_interval >= run_log.ABANDONED_AFTER.total_seconds():
            raise ValueError(
                "heartbeat_interval must be below runs.ABANDONED_AFTER, or a live "
                "run is indistinguishable from an abandoned one"
            )
        if self.flush_every < 1 or self.batch_limit < 1:
            raise ValueError("batch_limit and flush_every must be at least 1")


@dataclass
class CycleReport:
    """What one pass over the assignment list did. Facts, not a verdict."""

    sources_considered: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    sources_deferred: int = 0
    messages_seen: int = 0
    links_stored: int = 0
    watermarks_advanced: int = 0
    failures: list[tuple[int, FailureKind]] = field(default_factory=list)


@dataclass
class _Deferral:
    """A source this worker is not touching yet, and until when.

    ``until is None`` means "not again in this process": the retry policy
    is out of attempts, and the operator action recorded on that policy is
    what has to happen instead. Held in memory rather than in a column
    because it is a statement about this worker's session, not about the
    source — a fresh process re-reads reality rather than inheriting a
    judgement it cannot check.
    """

    until: datetime | None
    kind: FailureKind
    attempts: int


@dataclass(frozen=True)
class _Window:
    """A requested historical range, in message ids.

    Ids rather than dates, because that is the only bound Telegram's
    pagination actually takes. The dates are carried alongside and written
    onto the run, so the record says what a person asked for as well as
    what the API was told.
    """

    from_id: int
    to_id: int | None = None
    range_from: datetime | None = None
    range_to: datetime | None = None


@dataclass
class _Outcome:
    """The result of one source's run, as the cycle needs to see it."""

    messages_seen: int = 0
    links_stored: int = 0
    advanced: bool = False
    kind: FailureKind | None = None
    detail: str | None = None
    exc: BaseException | None = None


class AccountWorker:
    """The collecting loop for exactly one Telegram account.

    Owns no other account's state and shares no session with one, so a
    failure here is bounded to this account by construction rather than by
    a try/except somebody has to remember to write.
    """

    def __init__(
        self,
        *,
        workspace_id: int,
        account_id: int,
        reader: SourceReader,
        session_factory: Callable[[], Session],
        config: WorkerConfig | None = None,
        metrics: RuntimeMetrics | None = None,
        keyword_rules: list | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.account_id = account_id
        self.config = config or WorkerConfig()
        self.metrics = metrics or RuntimeMetrics()
        self._reader = reader
        self._session_factory = session_factory
        self._keyword_rules = keyword_rules
        self._stopping = asyncio.Event()
        self._queue: asyncio.Queue[tuple[str, IncomingMessage] | None] = asyncio.Queue(
            maxsize=self.config.queue_size
        )
        self._deferred: dict[int, _Deferral] = {}
        #: Telegram ref (peer id or @username) -> channels.id, rebuilt each
        #: cycle. The live handler runs outside any transaction and must
        #: not query, so the mapping is prepared where a session exists.
        self._routes: dict[str, int] = {}
        #: Set when Telegram asks the whole account to wait.
        self._paused_until: datetime | None = None
        self._reader.on_message(self._on_live_message)

    # -- session ---------------------------------------------------------

    @contextlib.contextmanager
    def _session(self):
        """A session bound to this workspace for its whole life.

        Every table this worker touches is under row-level security.
        Without the tenant the inserts are refused and the selects return
        nothing — silently, which is how a runtime ends up reporting
        healthy cycles while storing zero rows (see ``app/rls.py``).
        """
        db = self._session_factory()
        try:
            scope_session_to_workspace(db, self.workspace_id)
            yield db
        finally:
            db.close()

    # -- live path -------------------------------------------------------

    def _on_live_message(self, source_ref: str, message: IncomingMessage) -> None:
        """Called by the reader, synchronously, off its update loop.

        Does the least possible: no database, no blocking, no awaiting. A
        handler that blocks stalls the Telegram client itself, and a
        stalled client stops delivering for every source at once.
        """
        self.metrics.live_delivered += 1
        try:
            self._queue.put_nowait((source_ref, message))
        except asyncio.QueueFull:
            self.metrics.live_dropped += 1
            logger.warning(
                "account %s: live queue full, dropping message %s from %s "
                "(the sweep will read it again from the watermark)",
                self.account_id,
                message.message_id,
                source_ref,
            )

    async def drain_live(self) -> None:
        """Ingest queued live messages. Never touches a watermark."""
        while True:
            item = await self._queue.get()
            try:
                if item is None:  # shutdown sentinel
                    return
                source_ref, message = item
                channel_id = self.route(source_ref)
                if channel_id is None:
                    self.metrics.live_unassigned += 1
                    continue
                try:
                    await asyncio.to_thread(self._store_live, channel_id, message)
                except Exception as exc:  # noqa: BLE001 - isolation is the point
                    kind = failure_taxonomy.classify(exc)
                    self.metrics.failure(kind.value)
                    logger.warning(
                        "account %s: live ingest failed for source %s (%s): %s",
                        self.account_id,
                        channel_id,
                        kind.value,
                        exc,
                    )
            finally:
                self._queue.task_done()

    def route(self, source_ref: str) -> int | None:
        """Which assigned source a live message belongs to, if any."""
        if source_ref in self._routes:
            return self._routes[source_ref]
        key = canonical_id(source_ref)
        if key is not None and key in self._routes:
            return self._routes[key]
        return self._routes.get(source_ref.lstrip("@").lower())

    def _store_live(self, channel_id: int, message: IncomingMessage) -> None:
        """Blocking ingest of one live message, in a session of its own.

        A session is not safe to share across tasks, so the drain does not
        borrow the sweep's — and this runs in a thread because the ingest
        is synchronous and the loop is also delivering updates.
        """
        with self._session() as db:
            channel = db.get(Channel, channel_id)
            if channel is None:
                return
            summary = _ingest(db, channel, message, self._keyword_rules)
            db.commit()
        self.metrics.links_stored += summary.stored
        self.metrics.messages_seen += 1
        # Measured after the commit, so the number is "posted to durably
        # stored" rather than "posted to received" — the second is easy and
        # says nothing about whether the message survived.
        if message.posted_at is not None:
            posted = message.posted_at.replace(tzinfo=None)
            self.metrics.observe_live_lag(max(0.0, (utcnow() - posted).total_seconds()))

    # -- the sweep -------------------------------------------------------

    def assigned_sources(self, db: Session) -> list[Channel]:
        """Sources this account is responsible for, per the authority.

        Reads ``source_assignments`` — not ``channels.account_id``, which
        is a mirror kept for readers that have not moved yet. A worker
        reading the mirror would be one stray write away from collecting
        against an assignment that no longer exists.
        """
        return list(
            db.execute(
                select(Channel)
                .join(SourceAssignment, SourceAssignment.source_id == Channel.id)
                .where(
                    SourceAssignment.account_id == self.account_id,
                    SourceAssignment.released_at.is_(None),
                    Channel.workspace_id == self.workspace_id,
                )
                # Least recently looked at first, so a quiet source cannot
                # be starved by a busy one holding the front of the queue.
                .order_by(Channel.last_collected_at.is_(None).desc(), Channel.last_collected_at.asc())
            )
            .scalars()
            .all()
        )

    async def cycle(self, *, now: datetime | None = None) -> CycleReport:
        """One pass over every assigned source. The unit tests drive this.

        Separate from ``run()`` so the loop's behaviour can be proved
        without a scheduler: a test calls ``cycle()`` and asserts what
        moved, rather than starting a process and waiting for it.
        """
        report = CycleReport()
        moment = now or utcnow()

        if self._paused_until is not None and moment < self._paused_until:
            self.metrics.rate_limit_pauses += 1
            return report
        self._paused_until = None

        source_ids = await asyncio.to_thread(self._refresh_routes)
        report.sources_considered = len(source_ids)

        for channel_id in source_ids:
            if self._stopping.is_set():
                break
            deferral = self._deferred.get(channel_id)
            if deferral is not None and (deferral.until is None or moment < deferral.until):
                report.sources_deferred += 1
                continue
            await self._collect_one(channel_id, report, moment)

        return report

    def _refresh_routes(self) -> list[int]:
        """Re-read the assignment list, and rebuild the live routing map."""
        with self._session() as db:
            sources = self.assigned_sources(db)
            self._routes = _routes_for(sources)
            return [channel.id for channel in sources]

    async def _collect_one(self, channel_id: int, report: CycleReport, moment: datetime) -> None:
        """Collect one source, or record precisely why it did not happen."""
        try:
            outcome = await self._run_source(channel_id, moment)
        except Exception as exc:  # noqa: BLE001 - one source must not end the cycle
            kind = failure_taxonomy.classify(exc)
            logger.warning("account %s: source %s failed (%s): %s", self.account_id, channel_id, kind.value, exc)
            outcome = _Outcome(kind=kind, detail=str(exc), exc=exc)

        if outcome.kind is not None:
            self._record_failure(channel_id, outcome, report)
            return

        self._deferred.pop(channel_id, None)
        report.runs_completed += 1
        report.messages_seen += outcome.messages_seen
        report.links_stored += outcome.links_stored
        if outcome.advanced:
            report.watermarks_advanced += 1

    async def _run_source(
        self,
        channel_id: int,
        moment: datetime,
        *,
        track: str = SourceProgress.LIVE,
        window: _Window | None = None,
    ) -> _Outcome:
        """Open a run, fetch on the loop, store in a thread, then close it."""
        opened = await asyncio.to_thread(self._open_run, channel_id, track, window)
        if isinstance(opened, _Outcome):
            return opened
        run_id, watermark, ref = opened

        state = _FetchState(highest=watermark)
        try:
            await self._fetch_into(
                ref, channel_id, run_id, watermark, state, max_id=window.to_id if window else None
            )
        except BaseException as exc:  # noqa: BLE001 - classified, then recorded
            kind = failure_taxonomy.classify(exc)
            await asyncio.to_thread(
                self._fail_run, run_id, kind, str(exc), state.messages_seen, state.links_stored
            )
            return _Outcome(
                messages_seen=state.messages_seen,
                links_stored=state.links_stored,
                kind=kind,
                detail=str(exc),
                exc=exc,
            )

        return await asyncio.to_thread(self._finish_run, channel_id, run_id, state, moment, track)

    async def _fetch_into(
        self,
        ref: str,
        channel_id: int,
        run_id: int,
        watermark: int,
        state: _FetchState,
        *,
        max_id: int | None = None,
    ) -> None:
        """Read from the watermark forward, storing in bounded chunks.

        The iterator runs on the event loop because that is where the
        Telegram client lives; each chunk is handed to a thread to store.
        An exception anywhere leaves ``state.scope_examined`` false, which
        is what stops a partial read from being recorded as a completed
        one.
        """
        chunk: list[IncomingMessage] = []
        last_beat = utcnow()
        async for message in self._reader.fetch(
            ref, min_id=watermark, max_id=max_id, limit=self.config.batch_limit
        ):
            if self._stopping.is_set():
                # Asked to stop mid-range. Everything read so far is stored
                # and the watermark may still advance over it — it is
                # contiguous from where the run began — but the run did not
                # examine the range it asked for, and must not say it did.
                state.stopped_early = True
                break
            chunk.append(message)
            state.messages_seen += 1
            state.highest = max(state.highest, message.message_id)
            if len(chunk) >= self.config.flush_every:
                state.links_stored += await asyncio.to_thread(self._store_chunk, channel_id, chunk)
                chunk = []
                if (utcnow() - last_beat).total_seconds() >= self.config.heartbeat_interval:
                    await asyncio.to_thread(self._beat, run_id)
                    last_beat = utcnow()
        if chunk:
            state.links_stored += await asyncio.to_thread(self._store_chunk, channel_id, chunk)
        # Only a loop that ran out of messages examined its range. ``break``
        # falls through to here as surely as exhaustion does, so the flag is
        # derived from why the loop ended rather than from reaching this
        # line — an interrupted run that sets it is a run claiming to have
        # read a range it stopped part way through.
        state.scope_examined = not state.stopped_early

    # -- the blocking halves ---------------------------------------------

    def _open_run(
        self, channel_id: int, track: str = SourceProgress.LIVE, window: _Window | None = None
    ) -> tuple[int, int, str] | _Outcome:
        """Check ownership, note the attempt, and open a committed run."""
        with self._session() as db:
            channel = db.get(Channel, channel_id)
            if channel is None:
                return _Outcome(kind=FailureKind.SOURCE_UNAVAILABLE, detail="source row is gone")

            # Ownership, before any Telegram traffic. This is the cheap
            # check; the authoritative one happens again at write time,
            # because the assignment can move during the fetch.
            if assignments.current_account_id(db, channel.id) != self.account_id:
                self.metrics.assignment_lost += 1
                return _Outcome(
                    kind=FailureKind.ASSIGNMENT_CHANGED,
                    detail="assignment moved before the run started",
                )

            row = progress.record_attempt(db, channel, track)
            watermark = row.current_watermark
            if window is not None:
                # Resume, not restart. A backfill that has already reached
                # id 500 of its window continues from 500; one that has
                # never run starts at the window's lower bound.
                watermark = max(watermark, window.from_id)
            run = run_log.start(
                db,
                channel,
                mode=track,
                account_id=self.account_id,
                watermark_before=watermark,
                range_from=window.range_from if window else None,
                range_to=window.range_to if window else None,
            )
            ref = _entity_ref(channel)
            # Committed before any fetch: a RUNNING row that exists only
            # in an uncommitted transaction is invisible to the recovery
            # sweep, so a crash here would leave no trace at all.
            db.commit()
            self.metrics.runs_started += 1
            return (run.id, watermark, ref)

    def _store_chunk(self, channel_id: int, chunk: list[IncomingMessage]) -> int:
        """Store one chunk and commit it. Persist, before anything advances."""
        stored = 0
        with self._session() as db:
            channel = db.get(Channel, channel_id)
            if channel is None:
                return 0
            for message in chunk:
                stored += _ingest(db, channel, message, self._keyword_rules).stored
            db.commit()
        return stored

    def _beat(self, run_id: int) -> None:
        with self._session() as db:
            run = db.get(CollectionRun, run_id)
            if run is not None:
                run_log.heartbeat(db, run)
                db.commit()

    def _fail_run(
        self, run_id: int, kind: FailureKind, detail: str, messages_seen: int, links_stored: int
    ) -> None:
        """Close a run that did not finish. Keeps what was already stored.

        A failed run is not a rollback of the work that succeeded before
        it; treating it as one would re-collect data already on disk.
        """
        with self._session() as db:
            run = db.get(CollectionRun, run_id)
            if run is not None:
                run_log.fail(
                    db,
                    run,
                    kind=kind,
                    detail=detail,
                    messages_seen=messages_seen,
                    links_stored=links_stored,
                )
                db.commit()

    def _finish_run(
        self,
        channel_id: int,
        run_id: int,
        state: _FetchState,
        moment: datetime,
        track: str = SourceProgress.LIVE,
    ) -> _Outcome:
        """Move the watermark and close the run, in one transaction.

        Both or neither. A watermark whose run says nothing produced it,
        or a completed run whose watermark never moved, is a record that
        cannot be reconciled with the data afterwards.
        """
        with self._session() as db:
            channel = db.get(Channel, channel_id)
            run = db.get(CollectionRun, run_id)
            if channel is None or run is None:
                return _Outcome(kind=FailureKind.WORKER_FAILURE, detail="run or source vanished mid-run")

            result = progress.advance(
                db,
                channel,
                state.highest,
                account_id=self.account_id,
                track=track,
                run_id=run_id,
                at=moment,
                # A batch that came back short reached the end of what
                # Telegram had, so the examined range is contiguous from
                # the previous watermark. A full batch means there is
                # more, and this run says nothing about what it did not
                # read — which is what UNKNOWN_COVERAGE means.
                coverage=(
                    SourceProgress.NO_DETECTED_GAP
                    if state.scope_examined and state.messages_seen < self.config.batch_limit
                    else SourceProgress.UNKNOWN_COVERAGE
                ),
            )

            if result.refused is not None:
                kind = (
                    FailureKind.ASSIGNMENT_CHANGED
                    if result.refused == progress.STALE_ASSIGNMENT
                    else FailureKind.WATERMARK_CONFLICT
                )
                self.metrics.refusal(result.refused)
                if kind is FailureKind.ASSIGNMENT_CHANGED:
                    self.metrics.assignment_lost += 1
                run_log.fail(
                    db,
                    run,
                    kind=kind,
                    detail=result.refused,
                    messages_seen=state.messages_seen,
                    links_stored=state.links_stored,
                )
                db.commit()
                return _Outcome(
                    messages_seen=state.messages_seen,
                    links_stored=state.links_stored,
                    kind=kind,
                    detail=result.refused,
                )

            if state.stopped_early:
                run_log.cancel(db, run, detail="worker stopped before the range was finished")
                self.metrics.runs_cancelled += 1
            else:
                run_log.complete(
                    db,
                    run,
                    messages_seen=state.messages_seen,
                    links_stored=state.links_stored,
                    watermark_after=result.watermark,
                    scope_examined=state.scope_examined,
                )
            db.commit()
            cancelled = run.state == CollectionRun.CANCELLED
            completed = run.state == CollectionRun.COMPLETED
            failure_kind = run.failure_kind

        if cancelled:
            # Not a failure and not a success. The messages read are stored,
            # the watermark reflects them, and the rest of the range is the
            # next run's to read.
            return _Outcome(
                messages_seen=state.messages_seen,
                links_stored=state.links_stored,
                advanced=result.advanced,
            )

        if not completed:
            # ``complete()`` refused: the scope was never examined, so it
            # recorded a failure instead. Report it as one. Defensive —
            # this worker reaches ``complete()`` only on a fetch that ran
            # to its end — and kept because the alternative is a call site
            # that silently disagrees with the record it just wrote.
            kind = FailureKind(failure_kind) if failure_kind else FailureKind.UNKNOWN_FAILURE
            return _Outcome(
                messages_seen=state.messages_seen,
                links_stored=state.links_stored,
                kind=kind,
                detail="run ended without examining its requested scope",
            )

        self.metrics.runs_completed += 1
        self.metrics.messages_seen += state.messages_seen
        self.metrics.links_stored += state.links_stored
        if result.advanced:
            self.metrics.progress_advanced += 1
        return _Outcome(
            messages_seen=state.messages_seen,
            links_stored=state.links_stored,
            advanced=result.advanced,
        )

    # -- failure handling -------------------------------------------------

    def _record_failure(self, channel_id: int, outcome: _Outcome, report: CycleReport) -> None:
        """Apply the retry policy for a failure, and act on what it decides.

        An account-wide condition is raised as ``AccountStopped`` rather
        than deferred per source: a revoked session is a fact about the
        account, and walking the rest of the assignment list to rediscover
        it costs Telegram traffic and proves nothing.
        """
        kind = outcome.kind
        assert kind is not None
        report.runs_failed += 1
        report.failures.append((channel_id, kind))
        report.messages_seen += outcome.messages_seen
        report.links_stored += outcome.links_stored
        self.metrics.failure(kind.value)

        if kind is FailureKind.AUTH_FAILURE:
            raise AccountStopped(kind, outcome.detail or "authentication failed")

        if kind is FailureKind.RATE_LIMITED:
            wait = (failure_taxonomy.retry_after(outcome.exc) if outcome.exc is not None else None) or timedelta(
                minutes=1
            )
            self._paused_until = utcnow() + wait
            logger.warning(
                "account %s: rate limited, pausing the whole account until %s",
                self.account_id,
                self._paused_until.isoformat(),
            )
            return

        previous = self._deferred.get(channel_id)
        attempts = (previous.attempts if previous is not None else 0) + 1
        policy = failure_taxonomy.policy_for(kind)
        delay = policy.delay_for(attempts)
        if delay is None:
            self._deferred[channel_id] = _Deferral(until=None, kind=kind, attempts=attempts)
            logger.warning(
                "account %s: source %s stopped after %d attempt(s) (%s/%s). Operator action: %s",
                self.account_id,
                channel_id,
                attempts,
                kind.value,
                policy.retry_class.value,
                policy.operator_action or "none defined",
            )
            return
        self._deferred[channel_id] = _Deferral(until=utcnow() + delay, kind=kind, attempts=attempts)

    def deferral_for(self, channel_id: int) -> _Deferral | None:
        """What this worker has decided about a source. For tests and status."""
        return self._deferred.get(channel_id)

    # -- historical -------------------------------------------------------

    async def backfill(
        self,
        channel_id: int,
        *,
        from_id: int = 0,
        to_id: int | None = None,
        range_from: datetime | None = None,
        range_to: datetime | None = None,
        now: datetime | None = None,
    ) -> _Outcome:
        """Collect one requested window of the past, resumably.

        Runs on the ``HISTORICAL`` track, which has its own watermark. That
        separation is the whole reason the track column exists: a backfill
        walking January and a live sweep taking today's messages have two
        independent frontiers, and forcing them through one number puts the
        backfill's cursor into the live watermark — after which the live
        reader resumes from January and everything in between is collected
        by neither.

        Bounded by ``batch_limit`` like any other run, so a long window is
        several calls. Each resumes at the HISTORICAL watermark, so calling
        it repeatedly walks the window forward and calling it again after
        the window is exhausted is a no-op that completes.

        **Not scheduled by anything yet.** The runtime has no queue of
        backfill requests; this is the operation, and deciding when to ask
        for it is the work of a later phase.
        """
        window = _Window(from_id=from_id, to_id=to_id, range_from=range_from, range_to=range_to)
        return await self._run_source(channel_id, now or utcnow(), track=SourceProgress.HISTORICAL, window=window)

    # -- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        """Ask for a graceful stop. Safe to call from any task."""
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def run(self) -> None:
        """Connect, sweep until asked to stop, then shut down cleanly.

        The ``finally`` is the contract: whatever ends this loop — a stop,
        an account failure, a cancellation — the queue drains within its
        grace period and the reader is disconnected. A worker that leaves
        a Telegram connection open is a worker whose replacement cannot
        connect.
        """
        await self._reader.connect()
        self.metrics.reader_connects += 1
        drain = asyncio.create_task(self.drain_live())
        try:
            while not self._stopping.is_set():
                try:
                    await self.cycle()
                except AccountStopped as stopped:
                    logger.error(
                        "account %s stopping: %s (%s)",
                        self.account_id,
                        stopped.detail,
                        stopped.kind.value,
                    )
                    await asyncio.to_thread(self._mark_account_state, stopped)
                    raise
                await self._sleep_or_stop(self.config.cycle_pause)
        finally:
            self._stopping.set()
            await self._shutdown(drain)

    async def _shutdown(self, drain: asyncio.Task[None]) -> None:
        """Finish what is in flight, within a bounded grace period."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(drain, timeout=self.config.shutdown_grace)
        except TimeoutError:
            logger.warning(
                "account %s: %d live message(s) still queued at the shutdown deadline; "
                "they are not lost — the next sweep reads them from the watermark",
                self.account_id,
                self._queue.qsize(),
            )
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
        except asyncio.CancelledError:
            drain.cancel()
            raise
        finally:
            with contextlib.suppress(Exception):
                await self._reader.disconnect()
                self.metrics.reader_disconnects += 1

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Wait, but wake on a stop rather than at the deadline."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def _mark_account_state(self, stopped: AccountStopped) -> None:
        """Record on the account why its worker stopped.

        Written to the account rather than only logged: the control plane
        picks assignments by account state, and an account whose session
        was revoked must stop being eligible without waiting for a person
        to notice a log line.
        """
        from app import accounts as account_service
        from app.models import TelegramAccount

        try:
            with self._session() as db:
                account = db.get(TelegramAccount, self.account_id)
                if account is None:
                    return
                account_service.set_state(
                    db,
                    account,
                    TelegramAccount.AUTH_REQUIRED,
                    reason=(stopped.detail or "")[:200] or None,
                    commit=True,
                )
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.exception("account %s: could not record the stopping state", self.account_id)


@dataclass
class _FetchState:
    """What a fetch has accumulated so far, visible to its failure path."""

    highest: int
    messages_seen: int = 0
    links_stored: int = 0
    #: True only once the iterator has run to its end. A partial read is
    #: not a scope that was examined.
    scope_examined: bool = False
    #: Set when a shutdown interrupted the range rather than an error. The
    #: distinction matters to whoever reads the run later: cancelled on
    #: purpose is not the same fact as failed.
    stopped_early: bool = False


# -- helpers ---------------------------------------------------------------


def _entity_ref(channel: Channel) -> str:
    """How to address this source to Telegram, preferring its @username."""
    return channel.username or channel.tg_channel_id


def _routes_for(channels: Iterable[Channel]) -> dict[str, int]:
    """Every spelling a live message might arrive under, mapped to a row.

    Telethon reports ``chat_id`` in its own form, the dashboard stores
    whatever the operator typed, and a username may arrive in any case.
    All three are indexed rather than one canonical form, because a live
    message that fails to route is a message silently not stored.
    """
    routes: dict[str, int] = {}
    for channel in channels:
        raw = (channel.tg_channel_id or "").strip()
        if raw:
            routes[raw] = channel.id
            key = canonical_id(raw)
            if key is not None:
                routes[key] = channel.id
        if channel.username:
            routes[channel.username.lstrip("@").lower()] = channel.id
    return routes


def _ingest(db: Session, channel: Channel, message: IncomingMessage, keyword_rules: list | None):
    """One message into storage, through the existing ingest path.

    Reuses ``app.ingest`` rather than reimplementing storage: message
    identity, link extraction, classification and the duplicate rules all
    live there and are already proved by their own tests. This runtime's
    job is *when* and *by whom*, not how a link is stored.
    """
    return ingest_text(
        db,
        workspace_id=channel.workspace_id,
        channel_id=channel.id,
        text=message.text,
        message_id=message.message_id,
        posted_at=message.posted_at.replace(tzinfo=None) if message.posted_at else None,
        extra_urls=list(message.hidden_urls) or None,
        button_urls=list(message.button_urls) or None,
        forwarded_from=message.forwarded_from,
        sender_id=message.sender_id,
        sender_username=message.sender_username,
        sender_name=message.sender_name,
        channel_title=channel.title,
        keyword_rules=keyword_rules,
    )


__all__ = [
    "AccountStopped",
    "AccountWorker",
    "CycleReport",
    "WorkerConfig",
]
