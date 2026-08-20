"""Shared vocabulary for link vitality.

The scheduled checker (``scripts/check_link_vitality.py``) writes this
state and the API reads it, so the constants and the human-facing
interpretation live here rather than being duplicated on both sides with
a chance to disagree.
"""

from __future__ import annotations

# Statuses that say "the server could not answer right now", not "this
# link is gone". 5xx is treated the same way by the checker; these are the
# 4xx exceptions that are equally about the server's state, not the
# resource's existence.
#
#   408 Request Timeout      — the server gave up waiting, not a missing page
#   425 Too Early            — replay protection, retry later
#   429 Too Many Requests    — the checker itself is being throttled
#
# Getting 429 wrong is the expensive one: one popular host rate-limiting a
# batch used to mark every link on that host dead in a single run.
TRANSIENT_STATUSES = frozenset({408, 425, 429})

# How many consecutive unreachable probes before a link that was never
# given a definite 4xx is finally recorded as dead. Three is a judgement,
# not a measurement: with the six-hourly schedule it means roughly a day
# of sustained failure, which is long enough to outlast an outage and
# short enough to be useful.
UNREACHABLE_DEATH_THRESHOLD = 3


def status_category(http_status: int | None, is_alive: bool | None) -> str:
    """A stable, machine-readable label for one link's vitality.

    Returned to the API rather than a translated string so the caller
    decides the wording, and so the categories can be filtered on without
    parsing prose.
    """
    if is_alive is None and http_status is None:
        return "unchecked"
    if http_status is None:
        # Checked, but the request never produced a response at all.
        return "unreachable"
    if http_status < 300:
        return "ok"
    if http_status < 400:
        return "redirect"
    if http_status in (401, 403):
        return "blocked"
    if http_status in (404, 410):
        return "missing"
    if http_status in TRANSIENT_STATUSES:
        return "throttled"
    if http_status >= 500:
        return "server_error"
    return "client_error"
