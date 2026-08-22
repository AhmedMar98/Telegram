"""Machine-readable error codes for rejected requests.

A client that has to match on English prose to tell "unknown category"
from "too many saved searches" breaks the day the wording improves. The
prose stays — a human reads it — and a stable code now rides alongside it.

**The code goes in a header, not in ``detail``.** Phase 7's rule is no
breaking changes to existing endpoints, and every rejection this project
has ever returned carried ``detail`` as a plain string; several tests and
the dashboard read it that way. Turning it into an object would break all
of them to add a field a header can carry just as well.

The code *values* are the compatibility surface. Adding one is safe;
renaming one is a breaking change, governed by ``docs/16-api-policy.md``.
"""

from __future__ import annotations

from fastapi import HTTPException, status

ERROR_CODE_HEADER = "X-Error-Code"


class ErrorCode:
    UNKNOWN_CATEGORY = "unknown_category"
    UNKNOWN_SORT = "unknown_sort"
    UNKNOWN_FILTER = "unknown_filter"
    SAVED_SEARCH_LIMIT = "saved_search_limit"
    API_KEY_LIMIT = "api_key_limit"
    MALFORMED_BODY = "malformed_body"
    TOTP_REQUIRED = "totp_required"
    TOTP_INVALID = "totp_invalid"
    UNKNOWN_ALERT_TYPE = "unknown_alert_type"
    WEAK_PASSWORD = "weak_password"
    NOT_REDIRECTABLE = "not_redirectable"
    RATE_LIMITED = "rate_limited"
    LOGIN_THROTTLED = "login_throttled"
    WEBHOOK_REFUSED = "webhook_refused"
    # Not a client mistake and not a defect: every database connection was
    # in use and this request's wait ran out. Carried on a 503 so a client
    # can tell "retry this" apart from "this will never work".
    SERVER_BUSY = "server_busy"


def unprocessable(code: str, message: str) -> HTTPException:
    """422 with a stable code in the header and prose in ``detail``."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=message,
        headers={ERROR_CODE_HEADER: code},
    )


def coded_headers(code: str, *, retry_after_seconds: int | None = None) -> dict[str, str]:
    """The header pair every coded error carries, built in one place.

    Small enough to inline, which is exactly why it drifts: the 503 pool
    handler in ``app/main.py`` assembled this dict by hand and became the
    only construction of ``ERROR_CODE_HEADER`` outside this module. A
    convention with two implementations is a convention with two futures.
    """
    headers = {ERROR_CODE_HEADER: code}
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(retry_after_seconds)
    return headers


def rate_limited(code: str, message: str, *, retry_after_seconds: int) -> HTTPException:
    """429 that tells the client *when* to come back.

    A 429 with no ``Retry-After`` leaves a client guessing, and the usual
    guess is "immediately", which is precisely the behaviour the throttle
    exists to stop. Both throttles here have a known window, so the value
    is a fact rather than an estimate.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=message,
        headers=coded_headers(code, retry_after_seconds=retry_after_seconds),
    )
