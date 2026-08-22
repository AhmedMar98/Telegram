"""A short-lived, in-process cache for the one endpoint that earned it.

**This exists because of a measurement, not a hunch.** Phase 0 measured
``/links/stats`` at 25–35 ms over 20,000 links and concluded — correctly
for that number — that phase 11 had no case. Re-measured at the free
tier's actual storage ceiling (1,200,000 links, exactly 1.00 GiB) the same
endpoint takes **972 ms**, and a per-query breakdown accounts for all of
it::

    added_this_month   308 ms      deadest domains    123 ms
    by_category        181 ms      added_this_week     93 ms
    vitality           171 ms      top_domains         68 ms
                                   total_links         36 ms

That is not an N+1 problem — it is twelve queries, a fixed number, each
sweeping roughly 1.2M rows. No amount of query merging removes the sweep,
which is why this is a cache and not a rewrite.

It is worth caching specifically because the dashboard requests it on
every page load *and* after every mutation, while the underlying numbers
change on a six-hourly collector schedule.

**Three honest limits, stated rather than discovered:**

1. **In-process.** The collector is a separate process (a scheduled
   GitHub Actions job, not a worker), so a collector run cannot invalidate
   this cache. After one, stats can be up to ``ttl`` seconds stale. With a
   six-hourly schedule and a 30-second TTL that is not a real window.
2. **Per-worker.** Two web workers keep two caches. They can briefly
   disagree; both are bounded by the same TTL.
3. **Cold after sleep.** The free tier sleeps after 15 minutes idle, so
   the first request back always pays full price. This cache makes a
   session faster; it does not make a cold start faster, and nothing in
   this project can.

The existing ETag on the endpoint is unaffected and still does its own
job: it saves *bandwidth* when the body is unchanged. This saves the
*database work* that produced the body. They are complementary, and the
endpoint's docstring already said the ETag was not saving the latter.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# Long enough that a burst of dashboard activity — add a link, watch the
# counters, add another — is served from memory; short enough that a
# number a person is actively watching is never visibly wrong for long.
DEFAULT_TTL_SECONDS = 30.0

_lock = threading.Lock()
_entries: dict[int, tuple[float, Any]] = {}


def get(workspace_id: int, *, ttl: float = DEFAULT_TTL_SECONDS, now: float | None = None) -> Any | None:
    """The cached value for this workspace, or None if absent or stale.

    ``now`` is a test seam. Sleeping for 30 seconds to prove an expiry
    works is a test that nobody will keep.
    """
    if ttl <= 0:
        return None
    moment = time.monotonic() if now is None else now
    with _lock:
        entry = _entries.get(workspace_id)
        if entry is None:
            return None
        stored_at, value = entry
        if moment - stored_at > ttl:
            # Dropped rather than left to rot: a workspace that stops
            # being used should stop occupying memory, and this is the
            # only moment we are certain its entry is worthless.
            del _entries[workspace_id]
            return None
        return value


def put(workspace_id: int, value: Any, *, now: float | None = None) -> None:
    with _lock:
        _entries[workspace_id] = (time.monotonic() if now is None else now, value)


def invalidate(workspace_id: int) -> None:
    """Drop this workspace's entry after a write that changes the answer.

    Called from the request paths that mutate links. It cannot cover the
    collector — different process — which is limit 1 in the module
    docstring, not an oversight here.
    """
    with _lock:
        _entries.pop(workspace_id, None)


def clear() -> None:
    """Empty the cache. For tests, and for nothing else."""
    with _lock:
        _entries.clear()


def size() -> int:
    """How many workspaces are cached. For tests and diagnostics."""
    with _lock:
        return len(_entries)
