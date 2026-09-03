"""What the runtime counts, and the one number it refuses to produce.

Counters are separate on purpose. A single "collection success rate"
would average together four things that need four different responses —
an account whose session was revoked, a source that went private, a
network blip, and a source that genuinely had nothing new — and the
average is high enough to look healthy in every one of those cases. The
phase contract forbids that number, and this module is where the
prohibition is kept rather than merely stated: there is no field to read
it from.

Two more distinctions the counters preserve because collapsing them is
how a runtime lies about itself:

- ``runs_completed`` counts runs that examined their scope. It is not
  ``runs_started``, and it is not "connected without raising".
- ``progress_advanced`` counts watermarks that actually moved.
  ``runs_completed - progress_advanced`` is the volume of "we looked and
  there was nothing new", which is a healthy state and must not be
  indistinguishable from "we looked and something went wrong".

These live in the worker process and die with it, which is why they are
not the audit trail. ``collection_runs`` is the durable record;
``app.collection.health`` reads it. This is the cheap in-process view an
operator gets from a log line or a status endpoint.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeMetrics:
    """Process-local counters. Cheap, additive, and never averaged here."""

    runs_started: int = 0
    runs_completed: int = 0
    runs_cancelled: int = 0
    #: Keyed by ``FailureKind.value``. Never summed into one "failures"
    #: number by this class — the caller may, knowing what it is doing.
    runs_failed: Counter[str] = field(default_factory=Counter)

    messages_seen: int = 0
    links_stored: int = 0

    #: Live messages handed over by the reader.
    live_delivered: int = 0
    #: Live messages dropped because the queue was full. Not data loss:
    #: the live path never advances a watermark, so the next sweep reads
    #: the same message from the watermark forward. It is latency loss,
    #: and it is counted because a rising number means the sweep is now
    #: doing work the live path was meant to absorb.
    live_dropped: int = 0
    #: End-to-end live lag: seconds between a message's Telegram timestamp
    #: and the moment it was stored. Kept as a total plus a count rather
    #: than an average, so folding two workers' numbers together stays
    #: arithmetic instead of averaging averages. Only messages that arrived
    #: with a timestamp are counted, which is why the count is separate
    #: from ``live_delivered``.
    live_lag_seconds_total: float = 0.0
    live_lag_samples: int = 0
    live_lag_max_seconds: float = 0.0
    #: Live messages for a source this worker is not assigned. Counted
    #: rather than ignored: a non-zero value means the account is in
    #: dialogs nobody assigned it, which is an access fact worth seeing.
    live_unassigned: int = 0

    progress_advanced: int = 0
    #: Keyed by the refusal reason from ``app.progress``.
    progress_refused: Counter[str] = field(default_factory=Counter)
    #: Sources skipped before any Telegram traffic because the assignment
    #: had moved. The cheap half of ownership revalidation working.
    assignment_lost: int = 0

    reader_connects: int = 0
    reader_disconnects: int = 0
    worker_restarts: int = 0
    #: Cycles a worker sat out because Telegram told it to wait.
    rate_limit_pauses: int = 0

    def failure(self, kind: str) -> None:
        self.runs_failed[kind] += 1

    def refusal(self, reason: str) -> None:
        self.progress_refused[reason] += 1

    @property
    def live_lag_mean_seconds(self) -> float | None:
        """Mean lag, or None when nothing has been measured.

        ``None`` rather than zero: "no messages carried a timestamp" and
        "every message arrived instantly" are different facts, and zero
        would report the second when the first is true.
        """
        if not self.live_lag_samples:
            return None
        return self.live_lag_seconds_total / self.live_lag_samples

    def observe_live_lag(self, seconds: float) -> None:
        self.live_lag_seconds_total += seconds
        self.live_lag_samples += 1
        self.live_lag_max_seconds = max(self.live_lag_max_seconds, seconds)

    def merge(self, other: RuntimeMetrics) -> None:
        """Fold a worker's counters into the supervisor's total."""
        for name, value in vars(other).items():
            mine = getattr(self, name)
            if isinstance(value, Counter):
                mine.update(value)
            elif name == "live_lag_max_seconds":
                # A maximum does not add up.
                setattr(self, name, max(mine, value))
            else:
                setattr(self, name, mine + value)

    def snapshot(self) -> dict[str, Any]:
        """A flat, loggable view. Counters stay broken out by key."""
        out: dict[str, Any] = {}
        for name, value in vars(self).items():
            if isinstance(value, Counter):
                out[name] = dict(value)
            else:
                out[name] = value
        out["live_lag_mean_seconds"] = self.live_lag_mean_seconds
        return out
