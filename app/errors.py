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
    WEAK_PASSWORD = "weak_password"
    NOT_REDIRECTABLE = "not_redirectable"
    RATE_LIMITED = "rate_limited"
    LOGIN_THROTTLED = "login_throttled"


def unprocessable(code: str, message: str) -> HTTPException:
    """422 with a stable code in the header and prose in ``detail``."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=message,
        headers={ERROR_CODE_HEADER: code},
    )


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
        headers={"Retry-After": str(retry_after_seconds), ERROR_CODE_HEADER: code},
    )
