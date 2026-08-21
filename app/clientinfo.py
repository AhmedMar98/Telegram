"""Where a request came from, as far as the platform can tell.

Extracted so there is exactly one implementation. The subtlety here is the
kind that drifts silently when copied: Render terminates TLS at its own
proxy, so ``request.client.host`` is the proxy rather than the visitor, and
the real address arrives in ``X-Forwarded-For``. A second copy that forgot
that would record every request as coming from the load balancer, and the
audit trail would look fine while saying nothing.

Everything here is **advisory**. A client can send ``X-Forwarded-For``
itself, so these values help a person recognise their own activity and are
never used to make an access decision.
"""

from __future__ import annotations

from fastapi import Request


def client_origin(request: Request) -> tuple[str | None, str | None]:
    """The caller's IP and User-Agent, as reported by the platform.

    Only the first entry of ``X-Forwarded-For`` is used — the rest are
    upstream proxies.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
    return (ip or None), request.headers.get("user-agent")


def client_ip(request: Request) -> str | None:
    """Just the address, for callers that do not need the User-Agent."""
    return client_origin(request)[0]
