"""What this process has been doing since it started.

Ideas 185 and 192. Deliberately in-memory and deliberately small: a
metrics stack is a background process and a second datastore, and this
project has neither. What is actually needed is narrower — "is the app
slow, and how much work is it doing?" — and a handful of counters answers
it without a dependency.

**Bounded by construction.** Durations are folded into running totals plus
a fixed-size window for percentiles, so memory does not grow with traffic.
An unbounded list of every request would be the classic version of this
that quietly becomes the outage.

**Resets on restart, and says so.** The free tier sleeps after inactivity,
so these numbers describe the current process rather than all of history.
Presenting them as lifetime totals would be the kind of claim this project
does not make — the uptime is reported alongside precisely so the window
is visible.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# Enough for a meaningful median and p95 without holding much: 512 floats
# is a few kilobytes, and the free tier has 512MB total.
WINDOW = 512


@dataclass
class _State:
    started_at: float = field(default_factory=time.monotonic)
    requests: int = 0
    errors: int = 0
    total_seconds: float = 0.0
    slowest_seconds: float = 0.0
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))


_state = _State()
# Uvicorn on the free tier runs a single worker, but a lock costs nothing
# here and makes the counters correct if that ever changes.
_lock = threading.Lock()


def record(duration_seconds: float, *, status_code: int) -> None:
    with _lock:
        _state.requests += 1
        if status_code >= 500:
            # Only 5xx. A 404 or a 422 is the API working as designed, and
            # counting those as errors would make the number meaningless.
            _state.errors += 1
        _state.total_seconds += duration_seconds
        _state.slowest_seconds = max(_state.slowest_seconds, duration_seconds)
        _state.recent.append(duration_seconds)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
    return values[index]


def snapshot() -> dict:
    """Current counters. Safe to call on a request path — no I/O."""
    with _lock:
        requests = _state.requests
        errors = _state.errors
        total = _state.total_seconds
        slowest = _state.slowest_seconds
        recent = sorted(_state.recent)
        uptime = time.monotonic() - _state.started_at

    return {
        # Named so nobody reads these as lifetime figures: the free tier
        # sleeps, and every number here restarts with the process.
        "process_uptime_seconds": round(uptime, 1),
        "requests_since_start": requests,
        "server_errors_since_start": errors,
        "mean_response_ms": round((total / requests) * 1000, 1) if requests else 0.0,
        "median_response_ms": round(_percentile(recent, 0.5) * 1000, 1),
        "p95_response_ms": round(_percentile(recent, 0.95) * 1000, 1),
        "slowest_response_ms": round(slowest * 1000, 1),
        "sampled_requests": len(recent),
    }


def reset() -> None:
    """For tests. Never called by the application."""
    global _state
    with _lock:
        _state = _State()
