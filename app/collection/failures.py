"""What went wrong, in operational terms rather than exception classes.

An exception class names what the library noticed. This names what the
operator has to do about it, and those are different taxonomies: a
``ConnectionError`` and a ``FloodWaitError`` are both "the request did not
work" and need opposite responses — reconnect immediately, or wait and do
not touch the account.

So every failure is classified into a kind, every kind belongs to a retry
class, and the retry class decides the policy. Nothing retries forever and
nothing retries once as a rule; both are answers to a question nobody
asked about this particular failure.

``UNKNOWN_FAILURE`` is a real answer and stays that way. Guessing a
classification for an unrecognised error is worse than recording that it
was not recognised: a wrong classification silently applies the wrong
policy, and nobody looks at it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class FailureKind(StrEnum):
    """Operational cause. Stored on ``CollectionRun.failure_kind``."""

    AUTH_FAILURE = "AUTH_FAILURE"
    ACCESS_FAILURE = "ACCESS_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TELEGRAM_ERROR = "TELEGRAM_ERROR"
    DATABASE_FAILURE = "DATABASE_FAILURE"
    WORKER_FAILURE = "WORKER_FAILURE"
    #: Not a failure of the work — the work stopped being this worker's.
    ASSIGNMENT_CHANGED = "ASSIGNMENT_CHANGED"
    WATERMARK_CONFLICT = "WATERMARK_CONFLICT"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class RetryClass(StrEnum):
    """What may be done about a kind."""

    #: Try again later; the condition is expected to clear on its own.
    TRANSIENT = "TRANSIENT"
    #: Retrying cannot help. Something has to change first.
    PERMANENT = "PERMANENT"
    #: Refused by policy or by Telegram; retrying is the wrong response.
    POLICY_BLOCKED = "POLICY_BLOCKED"
    #: A person has to act.
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


@dataclass(frozen=True)
class RetryPolicy:
    """How a retry class behaves. Bounded, always."""

    retry_class: RetryClass
    max_attempts: int
    #: Delay before attempt n, indexed from zero. The last entry repeats.
    backoff: tuple[timedelta, ...]
    #: What the operator is expected to do, when anything.
    operator_action: str | None = None

    def delay_for(self, attempt: int) -> timedelta | None:
        """Wait before ``attempt`` (1-based), or None when out of attempts."""
        if attempt > self.max_attempts:
            return None
        index = min(max(attempt - 1, 0), len(self.backoff) - 1)
        return self.backoff[index]


_TRANSIENT = RetryPolicy(
    RetryClass.TRANSIENT,
    max_attempts=5,
    backoff=(
        timedelta(seconds=5),
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(minutes=30),
    ),
)
# Rate limiting is transient, and its backoff is not ours to choose: when
# Telegram says how long to wait, that number wins over anything here.
# These are the fallback for when it does not say.
_RATE_LIMITED = RetryPolicy(
    RetryClass.TRANSIENT,
    max_attempts=4,
    backoff=(timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2)),
    operator_action="nothing; the account is being throttled and recovers on its own",
)
_PERMANENT = RetryPolicy(
    RetryClass.PERMANENT,
    max_attempts=0,
    backoff=(),
    operator_action="the condition must change before this can work",
)
_POLICY = RetryPolicy(
    RetryClass.POLICY_BLOCKED,
    max_attempts=0,
    backoff=(),
    operator_action="refused by policy; retrying is not the answer",
)
_MANUAL = RetryPolicy(
    RetryClass.MANUAL_REQUIRED,
    max_attempts=0,
    backoff=(),
    operator_action="a person has to re-authorise or re-establish access",
)
# Not retried blindly. An unrecognised failure gets one cautious retry —
# transient conditions are the common case — and then stops rather than
# hammering something nobody has diagnosed.
_UNKNOWN = RetryPolicy(
    RetryClass.TRANSIENT,
    max_attempts=1,
    backoff=(timedelta(minutes=5),),
    operator_action="unclassified; read the run detail before assuming it is safe to retry",
)

_POLICIES: dict[FailureKind, RetryPolicy] = {
    FailureKind.AUTH_FAILURE: _MANUAL,
    FailureKind.ACCESS_FAILURE: _MANUAL,
    FailureKind.RATE_LIMITED: _RATE_LIMITED,
    FailureKind.NETWORK_FAILURE: _TRANSIENT,
    FailureKind.SOURCE_UNAVAILABLE: _PERMANENT,
    FailureKind.TELEGRAM_ERROR: _TRANSIENT,
    FailureKind.DATABASE_FAILURE: _TRANSIENT,
    FailureKind.WORKER_FAILURE: _TRANSIENT,
    # Not retried: the work belongs to somebody else now, and retrying it
    # here is precisely the stale write the ownership rule forbids.
    FailureKind.ASSIGNMENT_CHANGED: _POLICY,
    FailureKind.WATERMARK_CONFLICT: _MANUAL,
    FailureKind.TIMEOUT: _TRANSIENT,
    FailureKind.UNKNOWN_FAILURE: _UNKNOWN,
}


def policy_for(kind: FailureKind) -> RetryPolicy:
    return _POLICIES[kind]


#: Exception *names* rather than imported classes: telethon is an optional
#: dependency of the web process, and importing its exception tree here
#: would make classification unavailable exactly where a failure is most
#: likely to need naming.
_BY_EXCEPTION_NAME: dict[str, FailureKind] = {
    "FloodWaitError": FailureKind.RATE_LIMITED,
    "FloodError": FailureKind.RATE_LIMITED,
    "SlowModeWaitError": FailureKind.RATE_LIMITED,
    "AuthKeyError": FailureKind.AUTH_FAILURE,
    "AuthKeyUnregisteredError": FailureKind.AUTH_FAILURE,
    "SessionRevokedError": FailureKind.AUTH_FAILURE,
    "SessionExpiredError": FailureKind.AUTH_FAILURE,
    "UserDeactivatedError": FailureKind.AUTH_FAILURE,
    "UnauthorizedError": FailureKind.AUTH_FAILURE,
    "ChannelPrivateError": FailureKind.ACCESS_FAILURE,
    "ChatAdminRequiredError": FailureKind.ACCESS_FAILURE,
    "InviteHashExpiredError": FailureKind.ACCESS_FAILURE,
    "UserBannedInChannelError": FailureKind.ACCESS_FAILURE,
    "ChannelInvalidError": FailureKind.SOURCE_UNAVAILABLE,
    "UsernameNotOccupiedError": FailureKind.SOURCE_UNAVAILABLE,
    "UsernameInvalidError": FailureKind.SOURCE_UNAVAILABLE,
    "PeerIdInvalidError": FailureKind.SOURCE_UNAVAILABLE,
    "ConnectionError": FailureKind.NETWORK_FAILURE,
    "ConnectionResetError": FailureKind.NETWORK_FAILURE,
    "TimeoutError": FailureKind.TIMEOUT,
    "asyncio.TimeoutError": FailureKind.TIMEOUT,
    "OperationalError": FailureKind.DATABASE_FAILURE,
    "InterfaceError": FailureKind.DATABASE_FAILURE,
    "DBAPIError": FailureKind.DATABASE_FAILURE,
    "RpcCallFailError": FailureKind.TELEGRAM_ERROR,
    "RpcError": FailureKind.TELEGRAM_ERROR,
    "ServerError": FailureKind.TELEGRAM_ERROR,
}


def classify(exc: BaseException) -> FailureKind:
    """Name the operational cause of an exception.

    Walks the class hierarchy so a subclass of a known error is recognised
    without the table having to list every one Telethon defines. Anything
    unrecognised is ``UNKNOWN_FAILURE`` — which is an answer, not a gap to
    be filled with the nearest-looking guess.
    """
    for klass in type(exc).__mro__:
        kind = _BY_EXCEPTION_NAME.get(klass.__name__)
        if kind is not None:
            return kind
    return FailureKind.UNKNOWN_FAILURE


def retry_after(exc: BaseException) -> timedelta | None:
    """The wait Telegram itself asked for, when it asked for one.

    ``FloodWaitError`` carries ``seconds``. Honouring it is not politeness:
    retrying earlier is what turns a throttle into a restriction.
    """
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, int | float) and seconds > 0:
        return timedelta(seconds=float(seconds))
    return None
