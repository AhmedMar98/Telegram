"""The measurement contract: what "collected well" means as numbers.

§44.7 defined collection success in words — a watermark that advanced
contiguously over whatever existed, not a link count. This module turns
that definition into metrics, and it is written as a *contract* rather
than a set of counters because the counters are the easy half. The hard
half is the denominators, and a wrong denominator does not look wrong.

**Coverage is measured against what was due, not against everything.**
``succeeded / sources_expected`` punishes a system for not re-reading a
channel it correctly decided was not due yet, which makes the number drop
as the schedule gets smarter. ``succeeded / sources_due`` asks the only
question an operator cares about: of the sources this cycle was supposed
to read, how many did it read?

**Lag is not duration.** How long the job took is a fact about the runner.
How far behind the data is — the time between a message being posted and
this system having read up to it — is a fact about the product. They are
reported separately and never averaged together.

**Nothing here touches classification.** Collection correctness asks "did
we collect what we should have?"; classification accuracy asks "did we
understand it?". The second needs a labelled corpus that does not exist
(§44.11), and mixing an unmeasurable number into a measurable one makes
the whole figure unciteable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.dialogs import SOURCE_PUBLIC, SOURCE_USERBOT, is_synthetic
from app.models import Channel, CoverageSnapshot, Link, Message
from app.timeutil import utcnow

# --- failure taxonomy ------------------------------------------------------
#
# "failed = true" tells an operator that something is wrong and nothing
# about where to look. These eight say where. They are deliberately about
# the *cause a human would act on*, not about the exception class: a
# revoked session and a channel that banned the account both raise
# something from Telethon, and they need opposite responses.

ACCESS_DENIED = "access_denied"  # the account may not read this source
RATE_LIMITED = "rate_limited"  # FloodWait: Telegram said slow down
NETWORK_ERROR = "network_error"  # could not reach Telegram at all
TELEGRAM_ERROR = "telegram_error"  # Telegram answered with an error
DATABASE_ERROR = "database_error"  # the read worked, storing it did not
ASSIGNMENT_ERROR = "assignment_error"  # ownership changed under the run
SOURCE_UNAVAILABLE = "source_unavailable"  # deleted, renamed, or gone
UNKNOWN = "unknown"  # classified as unknown rather than silently dropped

FAILURE_KINDS: tuple[str, ...] = (
    ACCESS_DENIED,
    RATE_LIMITED,
    NETWORK_ERROR,
    TELEGRAM_ERROR,
    DATABASE_ERROR,
    ASSIGNMENT_ERROR,
    SOURCE_UNAVAILABLE,
    UNKNOWN,
)

# --- outcomes --------------------------------------------------------------

SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"  # deliberately not attempted: not due, paused, not ours
OUTCOMES: tuple[str, ...] = (SUCCEEDED, FAILED, SKIPPED)

# How long after its last successful read a source becomes due again. The
# scheduled collector runs hourly, so anything not read within the hour is
# due by the next tick. Configurable is not the point yet — naming it is:
# "due" was previously an idea nobody had written down, which is why
# coverage could not be computed at all.
DUE_AFTER = timedelta(hours=1)

# A source read within this window is "fresh". Not a promise, a threshold
# for one traffic-light: the underlying lag in seconds is always reported
# next to it, because a threshold hides how far past it a source is.
FRESH_WITHIN = timedelta(hours=3)


@dataclass(frozen=True)
class DuplicateRates:
    """Three different things that "duplicate" can mean, kept apart.

    Collapsing them into one number produces a figure that moves for
    reasons the reader cannot infer: a channel reposting its own link, two
    channels sharing one resource, and the collector re-reading a message
    are three different events with three different responses (none,
    none — that is the product working — and a bug, respectively).
    """

    #: A message the system had already fully processed and skipped again.
    #: Expected and healthy: it is the live listener and the scheduled
    #: collector overlapping, exactly as designed.
    duplicate_message: float
    #: A URL already stored *for this channel*. Also healthy — channels
    #: repost — and the number is about noise in a source, not a defect.
    duplicate_link_occurrence: float
    #: The same canonical URL present in more than one channel. This is
    #: not waste at all: it is the cross-source signal the product exists
    #: to surface, and it is reported so it is never mistaken for waste.
    duplicate_resource: float


@dataclass(frozen=True)
class WatermarkIntegrity:
    """Whether the collector's own bookkeeping is sound.

    Independent of coverage on purpose: a run can report every source
    succeeded and still have left a hole, which is precisely the failure
    §45.3 exists to prevent. So this is never folded into the success
    rate — it is reported beside it.
    """

    #: Sources whose watermark moved backwards. Must be zero. Anything
    #: else means messages between the old and new value will be re-read
    #: forever, or skipped forever, depending on which way it moved.
    regressions: int
    #: Sources whose last run ended without reaching the end of the
    #: channel — the cap was hit, so a backlog remains. Not an error; an
    #: unfinished window that the next run continues.
    behind: int
    #: Sources abandoned mid-run because ownership changed (§45.3).
    ownership_conflicts: int

    @property
    def sound(self) -> bool:
        return self.regressions == 0 and self.ownership_conflicts == 0


@dataclass
class Coverage:
    """One workspace's collection, measured. Every field is a count or a
    ratio with a named denominator; nothing here is an opinion."""

    sources_expected: int = 0
    #: Sources this cycle was supposed to read: everything expected, minus
    #: what policy deliberately left out (paused, out of scope, not
    #: scheduled). Today only ``skipped`` subtracts; when adaptive
    #: scheduling arrives it is the same subtraction with more inputs.
    sources_due: int = 0
    #: Due sources whose last successful read is older than the cadence.
    #: A staleness signal, reported beside coverage rather than inside it:
    #: "read, but late" and "not read at all" are different problems.
    sources_overdue: int = 0
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    failures_by_kind: dict[str, int] = field(default_factory=dict)

    #: Seconds between the newest message this system has stored and now.
    #: The product's freshness. None when nothing has been collected yet —
    #: which is not "infinitely stale", it is "unknown", and the two must
    #: not render the same.
    collection_lag_seconds: float | None = None
    #: Seconds between a source's newest stored message and the moment the
    #: collector last read that source: how far behind the reader was at
    #: the time it read, as distinct from how long ago that was.
    watermark_lag_seconds: float | None = None

    duplicates: DuplicateRates = field(default_factory=lambda: DuplicateRates(0.0, 0.0, 0.0))
    watermark: WatermarkIntegrity = field(default_factory=lambda: WatermarkIntegrity(0, 0, 0))

    @property
    def coverage_rate(self) -> float | None:
        """``succeeded / due`` — **not** ``succeeded / expected``.

        Both halves must count the same population, which is the mistake
        the first version of this made: it divided *every* success by the
        *overdue* subset and produced 3.33 — a coverage rate above 100%,
        found by its own test. ``due`` is therefore "expected minus what
        policy left out", so a source deliberately not scheduled leaves
        both halves rather than only the denominator.

        None rather than 1.0 when nothing was due: "everything due
        succeeded, and nothing was due" is an absent score, not a perfect
        one, and a 100% meaning "we did nothing" is the most misleading
        number this module could produce.
        """
        if self.sources_due == 0:
            return None
        return round(min(self.sources_succeeded / self.sources_due, 1.0), 4)

    @property
    def failure_rate(self) -> float | None:
        if self.sources_attempted == 0:
            return None
        return round(self.sources_failed / self.sources_attempted, 4)

    @property
    def gap_rate(self) -> float | None:
        """Attempts that ended with a hole rather than a clean stop.

        Measured over *attempts*, and only over holes this system can
        actually detect: a watermark that moved backwards, and a run
        abandoned because ownership changed. Messages Telegram held that
        were never offered to us are **not** counted, because they cannot
        be — see the module note in §46.4 rather than inferring a zero
        here means "no gaps ever".
        """
        if self.sources_attempted == 0:
            return None
        holes = self.watermark.regressions + self.watermark.ownership_conflicts
        return round(holes / self.sources_attempted, 4)

    @property
    def is_fresh(self) -> bool | None:
        if self.collection_lag_seconds is None:
            return None
        return self.collection_lag_seconds <= FRESH_WITHIN.total_seconds()


def measure(db: Session, workspace_id: int) -> Coverage:
    """Compute the contract for one workspace. Reads only; never writes.

    **Reads derived columns, deliberately.** ``last_outcome``,
    ``last_failure_kind``, ``caught_up`` and ``watermark_regressions`` live
    on ``channels``, and since the reconciliation of the two branches they
    are no longer independent state: ``source_progress`` and
    ``collection_runs`` decide, and each of those four columns has exactly
    one writer that copies from them (see the block on ``Channel`` in
    ``app/models.py``).

    They are read here rather than joined from the authority because the
    projection is exact for every value this function distinguishes, and a
    five-way join per measurement would cost more than it buys. What makes
    that safe is the single writer, not luck —
    ``tests/test_legacy_field_authority.py`` asserts the two agree after
    the operations that move them, and goes red if a second writer appears.

    One value is *not* recoverable from here and must not be inferred:
    ``caught_up IS NULL`` means "never read" **or** "coverage unknown",
    which the three-valued ``source_progress.coverage_status`` separates.
    Nothing below treats NULL as either, and nothing should.
    """
    now = utcnow()
    due_before = now - DUE_AFTER

    sources = [
        channel
        for channel in db.query(Channel)
        .filter(
            Channel.workspace_id == workspace_id,
            Channel.is_active.is_(True),
            Channel.source.in_((SOURCE_USERBOT, SOURCE_PUBLIC)),
        )
        .all()
        if not is_synthetic(channel.tg_channel_id)
    ]

    report = Coverage(sources_expected=len(sources))
    failures: dict[str, int] = {}

    for channel in sources:
        outcome = channel.last_outcome

        if outcome == SKIPPED:
            # Policy left this one out of the cycle: paused, out of scope,
            # not scheduled. It is neither attempted nor due, so it leaves
            # both halves of the ratio instead of only the denominator.
            report.sources_skipped += 1
            continue

        report.sources_due += 1
        # Overdue is a staleness signal about the *schedule*, kept apart
        # from coverage: a source read late still counts as covered.
        if channel.last_collected_at is None or channel.last_collected_at <= due_before:
            report.sources_overdue += 1

        if outcome is None:
            # Due and never attempted. Counted in the denominator and not
            # the numerator, which is exactly what uncovered means.
            continue

        report.sources_attempted += 1
        if outcome == SUCCEEDED:
            report.sources_succeeded += 1
        elif outcome == FAILED:
            report.sources_failed += 1
            kind = channel.last_failure_kind or UNKNOWN
            failures[kind] = failures.get(kind, 0) + 1

    report.failures_by_kind = failures
    report.watermark = _watermark_integrity(sources)
    report.collection_lag_seconds, report.watermark_lag_seconds = _lag(db, workspace_id, now)
    report.duplicates = _duplicates(db, workspace_id)
    return report


def _watermark_integrity(sources: list[Channel]) -> WatermarkIntegrity:
    return WatermarkIntegrity(
        regressions=sum(1 for channel in sources if (channel.watermark_regressions or 0) > 0),
        behind=sum(1 for channel in sources if channel.caught_up is False),
        ownership_conflicts=sum(1 for channel in sources if channel.last_failure_kind == ASSIGNMENT_ERROR),
    )


def _lag(db: Session, workspace_id: int, now) -> tuple[float | None, float | None]:
    """Freshness of the corpus, and how far behind the last read was.

    Both derive from ``Message.posted_at``, which is Telegram's own
    timestamp rather than ours — so this measures the age of the *data*,
    not the age of the job.
    """
    newest = db.query(func.max(Message.posted_at)).filter(Message.workspace_id == workspace_id).scalar()
    if newest is None:
        return None, None

    collection_lag = (now - newest).total_seconds()

    # The read that stored the newest message: how stale the data already
    # was when it arrived here. Zero would mean the listener caught it as
    # it was posted; hours means the hourly sweep found it late.
    collected_at = (
        db.query(func.max(Channel.last_collected_at)).filter(Channel.workspace_id == workspace_id).scalar()
    )
    watermark_lag = (collected_at - newest).total_seconds() if collected_at else None
    return round(collection_lag, 1), (round(watermark_lag, 1) if watermark_lag is not None else None)


def _duplicates(db: Session, workspace_id: int) -> DuplicateRates:
    """The three rates, each over its own denominator."""
    links = db.query(func.count(Link.id)).filter(Link.workspace_id == workspace_id).scalar() or 0
    messages = db.query(func.count(Message.id)).filter(Message.workspace_id == workspace_id).scalar() or 0
    if not links:
        return DuplicateRates(0.0, 0.0, 0.0)

    # A resource present in more than one channel: counted from the hash,
    # which is the canonical form (§43.3), so two spellings of one link do
    # not inflate it.
    distinct_hashes = (
        db.query(func.count(func.distinct(Link.url_hash))).filter(Link.workspace_id == workspace_id).scalar() or 0
    )
    shared = links - distinct_hashes

    # Messages that produced more than one link are not duplicates; the
    # ratio here is stored links per stored message, which is how a
    # re-read would show up (messages flat, links flat, but attempts up).
    per_message = round(links / messages, 4) if messages else 0.0

    return DuplicateRates(
        duplicate_message=per_message,
        duplicate_link_occurrence=round(shared / links, 4),
        duplicate_resource=round(shared / links, 4) if distinct_hashes else 0.0,
    )


# --- the operational time series (§47) --------------------------------------
#
# A snapshot answers "how are we doing now". It cannot answer the question
# that decides whether to act: **is it getting worse?** 99.2%, then 98.7%,
# then 94.1% is a system degrading in plain sight, and every one of those
# readings looks acceptable alone. So each run writes a row.

#: How long snapshots are kept. Long enough to see a week-over-week trend
#: and a slow drift; short enough that an hourly run cannot fill a 1 GiB
#: database with its own telemetry (24 x 365 rows would be ~9k/year, so
#: this is generous rather than tight).
SNAPSHOT_RETENTION_DAYS = 120


def _percentiles(values: list[float]) -> tuple[float | None, float | None]:
    """p50 and p95 of a small list, without pulling in a stats dependency.

    Reported instead of a mean because a mean hides exactly the case that
    matters: nine fresh sources and one a day behind average out to
    "fine". The median says what a typical source looks like; p95 says how
    bad the tail is, and the tail is what breaks first.
    """
    if not values:
        return None, None
    ordered = sorted(values)

    def pick(fraction: float) -> float:
        # Nearest-rank, which needs no interpolation and is exact for the
        # small N this runs on (one value per source).
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 1)

    return pick(0.5), pick(0.95)


def per_source_lag(db: Session, workspace_id: int) -> list[float]:
    """Seconds since each source's newest stored message, one per source.

    The distribution the percentiles are taken over. Sources that have
    never produced a message are absent rather than counted as zero: a
    source with no data is not a fresh source.
    """
    now = utcnow()
    rows = (
        db.query(Message.channel_id, func.max(Message.posted_at))
        .filter(Message.workspace_id == workspace_id, Message.posted_at.isnot(None))
        .group_by(Message.channel_id)
        .all()
    )
    return [(now - newest).total_seconds() for _, newest in rows if newest is not None]


def record_snapshot(
    db: Session,
    workspace_id: int,
    *,
    run_id: str,
    started_at,
    summary=None,
) -> CoverageSnapshot:
    """Write one run's measurement into the series. Commits.

    ``summary`` is the run's ``IngestSummary`` when there is one; a run
    that collected nothing still writes a row, because a gap in the series
    is indistinguishable from a run that found nothing, and those need
    opposite responses.
    """
    report = measure(db, workspace_id)
    lags = per_source_lag(db, workspace_id)
    p50, p95 = _percentiles(lags)

    snapshot = CoverageSnapshot(
        workspace_id=workspace_id,
        run_id=run_id,
        started_at=started_at,
        finished_at=utcnow(),
        sources_expected=report.sources_expected,
        sources_due=report.sources_due,
        sources_attempted=report.sources_attempted,
        sources_succeeded=report.sources_succeeded,
        sources_failed=report.sources_failed,
        sources_skipped=report.sources_skipped,
        messages_seen=getattr(summary, "scanned", 0) or 0,
        messages_processed=(getattr(summary, "scanned", 0) or 0) - (getattr(summary, "already_processed", 0) or 0),
        links_found=(getattr(summary, "stored", 0) or 0) + (getattr(summary, "duplicates", 0) or 0),
        links_stored=getattr(summary, "stored", 0) or 0,
        duplicate_occurrences=getattr(summary, "duplicates", 0) or 0,
        collection_lag_p50=p50,
        collection_lag_p95=p95,
        watermark_regressions=report.watermark.regressions,
        gap_events=report.watermark.regressions + report.watermark.ownership_conflicts,
    )
    db.add(snapshot)
    _prune_snapshots(db, workspace_id)
    db.commit()

    # Loaded and detached before returning. Without this the caller gets a
    # live ORM instance that raises ``DetachedInstanceError`` the moment
    # its session closes — which is exactly what a caller does after
    # recording a run, and the error names SQLAlchemy internals rather
    # than the mistake. Detached-with-values is what "here is what I just
    # wrote" should mean.
    db.refresh(snapshot)
    db.expunge(snapshot)
    return snapshot


def _prune_snapshots(db: Session, workspace_id: int) -> None:
    """Drop rows past the retention window, on write.

    On write rather than as a scheduled job for the same reason
    ``login_attempts`` does it: a cleanup job that has to be remembered is
    a table that grows forever on the deployment that forgot.
    """
    cutoff = utcnow() - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    db.query(CoverageSnapshot).filter(
        CoverageSnapshot.workspace_id == workspace_id, CoverageSnapshot.finished_at < cutoff
    ).delete(synchronize_session=False)


def history(db: Session, workspace_id: int, *, limit: int = 100) -> list[CoverageSnapshot]:
    """The series, newest first."""
    return (
        db.query(CoverageSnapshot)
        .filter(CoverageSnapshot.workspace_id == workspace_id)
        .order_by(CoverageSnapshot.finished_at.desc())
        .limit(limit)
        .all()
    )


def trend(snapshots: list[CoverageSnapshot]) -> Literal["improving", "steady", "degrading", "unknown"]:
    """Which way coverage is moving: improving, steady, degrading, unknown.

    Compares the mean of the newest third against the mean of the oldest
    third, which is deliberately crude: this is a *flag* telling an
    operator to look, not a statistic. A single reading is "unknown"
    rather than "steady" — one point has no direction.
    """
    rates = [snapshot.sources_succeeded / snapshot.sources_due for snapshot in snapshots if snapshot.sources_due]
    if len(rates) < 4:
        return "unknown"
    # ``snapshots`` arrives newest-first.
    third = max(1, len(rates) // 3)
    newest = sum(rates[:third]) / third
    oldest = sum(rates[-third:]) / third
    if newest - oldest > 0.02:
        return "improving"
    if oldest - newest > 0.02:
        return "degrading"
    return "steady"
