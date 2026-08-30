"""Refuse to fetch URLs that point back into private network space.

The vitality checker takes URLs collected from Telegram channels — content
this deployment does not control — and issues an HTTP request to each one
from inside the runner. Without a guard that is a server-side request
forgery primitive: a link to ``http://169.254.169.254/latest/meta-data/``
makes the checker read cloud instance metadata, and one to
``http://127.0.0.1:5432`` probes services that are only reachable from
inside. The response body is never stored, but the *status code* is, and
a status code is enough to enumerate what is listening.

Two properties make the check non-trivial, and both are handled here:

1. **The hostname is not the address.** ``evil.example.com`` can resolve
   to 127.0.0.1. Blocking on the literal string is useless; the name has
   to be resolved and the resulting addresses inspected.

2. **A redirect is a second request.** A URL that resolves publicly can
   answer 302 and point at a private address, so validating only the URL
   the checker started with protects nothing. Callers must therefore
   disable automatic redirect following and re-validate every hop — see
   ``scripts/check_link_vitality.py``, which does exactly that.

**Known residual limitation, stated rather than discovered later:** this
resolves the name and then hands the original URL to httpx, which resolves
it again when it connects. A name that answers with a public address on
the first lookup and a private one on the second (DNS rebinding) would
pass. Closing that requires pinning the checked address for the actual
connection, which httpx does not expose cleanly. The gap is narrow — it
needs an attacker running authoritative DNS with a near-zero TTL — and it
is far smaller than the hole it replaces, which was every private address
reachable with no attacker effort at all.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# Only these reach the network. A ``file://`` URL is not a reachability
# question, and ``gopher://``/``dict://`` are classic SSRF pivots for
# speaking other protocols through an HTTP client.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _address_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address must not be fetched, or None if it is fine.

    Each category is named separately in the return value because the
    reason ends up in a log line an operator reads: "link-local" tells
    them a metadata-endpoint probe was refused, which is worth knowing,
    while a generic "blocked" tells them nothing.
    """
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 and fe80::/10. This is the cloud metadata range.
        return "link-local address"
    if ip.is_private:
        # RFC1918 for v4, unique-local fc00::/7 for v6.
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) presents as global
    # unless it is unwrapped first, so unwrap and re-check.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _address_is_blocked(mapped)
    return None


async def check_url(url: str) -> str | None:
    """Why this URL must not be fetched, or None if it may be.

    Returns a human-readable reason rather than a bool so the caller can
    log *which* rule fired without re-deriving it.

    Every address the name resolves to is checked, not just the first: a
    host that answers with one public and one private address would
    otherwise pass and then be connected to on whichever the OS picked.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"scheme {scheme!r} is not http(s)"

    host = parts.hostname
    if not host:
        return "no host in URL"

    # A literal address needs no DNS round trip, and passing one to
    # getaddrinfo would work but wastes a lookup on every such link.
    try:
        return _address_is_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # A name that does not resolve is not an SSRF problem — it is an
        # ordinary dead link, and the caller's existing error handling
        # already reports that far better than this function could.
        return None

    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo returns valid addresses
            continue
        reason = _address_is_blocked(parsed)
        if reason is not None:
            return f"{reason} ({address})"
    return None
