"""Collection runtime: what a run is, how it fails, and whether it works.

``failures`` names the operational cause and the retry policy, ``runs``
owns a run's lifecycle and what it may claim, and ``health`` answers the
only question that matters afterwards — is collection actually working —
in findings rather than a score.
"""

from app.collection.failures import (
    FailureKind,
    RetryClass,
    classify,
    policy_for,
)
from app.collection.health import Finding, report
from app.collection.runs import (
    cancel,
    complete,
    fail,
    heartbeat,
    recover_abandoned,
    start,
)

__all__ = [
    "FailureKind",
    "Finding",
    "RetryClass",
    "cancel",
    "classify",
    "complete",
    "fail",
    "heartbeat",
    "policy_for",
    "recover_abandoned",
    "report",
    "start",
]
