"""An outbound webhook the workspace configures for itself (idea 162).

**Why this instead of integrations.** Slack, Discord, Notion, IFTTT and
everything like them each want an OAuth app, a stored third-party token,
and a maintained client. One HTTPS POST to an address the user supplies
reaches all of them through the incoming-webhook feature they already
have, costs nothing, and stores no third-party credential this project
would then be responsible for.

**This is the one place the service makes a request to an address a user
chose, so the whole module is about that.**

- **HTTPS only.** Not a style preference: the payload is the alert text,
  and the alert text is the thing being protected.
- **Every resolved address must be public.** Private, loopback,
  link-local, reserved, multicast and unspecified ranges are refused,
  and IPv4-mapped IPv6 forms like ``::ffff:127.0.0.1`` are unwrapped to
  the IPv4 address inside before the ranges are consulted.
- **Redirects are not followed.** A 302 to ``http://169.254.169.254`` is
  the classic bypass of everything above, and following it would undo both
  previous rules in one hop.
- **The response body is never read or returned.** Only the status code
  reaches the caller, and only for a target that already passed the
  public-address check — which is information the user could get with
  ``curl`` from their own machine, so it is not an oracle they did not
  already have.
- **Delivery is best effort and never raises.** A webhook failure must not
  affect the alert: the alert is the product, the webhook is a copy.

**The residual risk, stated rather than implied.** Addresses are checked
immediately before the request, but the HTTP client resolves the name
again itself, so a name that answers publicly during the check and
privately a moment later would slip through that window. Closing it
properly means pinning the connection to the checked address, which means
taking over TLS host verification. It is not closed here, and the reason
it is acceptable is specific: the only person who can configure the URL is
the workspace's own owner, the request carries their own alert text, and
they never see the response. What they would gain is a blind POST from a
free-tier web service, which is not worth the certificate handling it
would cost to prevent.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.crypto import InvalidToken, decrypt_field, encrypt_field
from app.models import Workspace
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

ALLOWED_SCHEME = "https"
TIMEOUT_SECONDS = 5.0
MAX_URL_LENGTH = 500

# The masked form shows the host and nothing else.
#
# The obvious alternative — keeping the last few characters, the way a
# card number is shown — was tried and dropped. Those characters are the
# tail of a secret token, and they buy nothing: a workspace has exactly
# one webhook, so there is no second one to tell it apart from. The
# project's existing precedent points the same way; an API key is shown by
# its *prefix* (app/apikeys.py), which is a deliberate non-secret handle,
# not by a slice of the secret itself.


class WebhookRefused(ValueError):
    """The URL cannot be used, with a reason meant for the person who typed it."""


def _public_form(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """An address with any IPv4-mapped IPv6 wrapper removed.

    **Measured, not assumed:** on the interpreter this ships against
    (CPython 3.11.15) ``IPv6Address("::ffff:127.0.0.1")`` already answers
    True to both ``is_loopback`` and ``is_private``, so the unwrapping
    below changes no verdict there — the naive check would refuse it too.

    It is kept anyway, and the reason is the version number in that
    sentence. How CPython classifies mapped addresses has been corrected
    more than once, so a check that relies on it is a check whose answer
    depends on a patch release. Unwrapping first makes the IPv4 rules
    apply to an IPv4 address however it happens to be spelled, which is
    the property actually wanted here.
    """
    address = ipaddress.ip_address(raw)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate(url: str) -> str:
    """Check a candidate URL, or raise WebhookRefused saying why.

    Called both when the URL is saved (so the answer is immediate) and
    again immediately before every send (so a name that starts resolving
    somewhere private stops working).
    """
    url = (url or "").strip()
    if not url:
        raise WebhookRefused("العنوان فارغ.")
    if len(url) > MAX_URL_LENGTH:
        raise WebhookRefused(f"العنوان أطول من {MAX_URL_LENGTH} محرفاً.")

    parsed = urlparse(url)
    if parsed.scheme != ALLOWED_SCHEME:
        raise WebhookRefused("يجب أن يبدأ العنوان بـ https:// — نصّ التنبيه هو ما تحميه هذه القاعدة.")
    if not parsed.hostname:
        raise WebhookRefused("العنوان بلا مضيف.")

    try:
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise WebhookRefused("تعذّر تحويل اسم المضيف إلى عنوان.") from None

    for entry in resolved:
        # sockaddr is (host, port) for IPv4 and a 4-tuple for IPv6, and its
        # first element is typed str | int; only the string form ever
        # reaches here in practice, and str() makes that explicit rather
        # than asserted.
        address = _public_form(str(entry[4][0]))
        if not _is_public(address):
            # The address is not named back: it is exactly the fact
            # somebody probing internal ranges is trying to learn.
            raise WebhookRefused("المضيف يشير إلى عنوان داخلي أو محجوز، ولا يُسمح بذلك.")

    return url


def mask(url: str) -> str:
    """A recognisable stand-in for a URL that must not be shown whole."""
    return f"https://{urlparse(url).hostname or '?'}/…"


def configured_url(workspace: Workspace) -> str | None:
    """The stored URL in plaintext, for sending. Never for returning."""
    if not workspace.webhook_url:
        return None
    try:
        return decrypt_field(workspace.webhook_url)
    except InvalidToken:
        # A wrong FIELD_ENCRYPTION_KEY, or a row from before encryption.
        # Silently not sending beats sending somewhere unintended.
        logger.warning("workspace %s: webhook URL could not be decrypted", workspace.id)
        return None


def store_url(db: Session, workspace: Workspace, url: str) -> str:
    """Validate then store. Returns the masked form, which is all a caller gets."""
    validated = validate(url)
    workspace.webhook_url = encrypt_field(validated)
    workspace.webhook_last_status = None
    workspace.webhook_last_attempt_at = None
    db.commit()
    return mask(validated)


def clear_url(db: Session, workspace: Workspace) -> None:
    workspace.webhook_url = None
    workspace.webhook_last_status = None
    workspace.webhook_last_attempt_at = None
    db.commit()


async def deliver(db: Session, workspace: Workspace, payload: dict[str, Any]) -> int | None:
    """POST one alert to the configured URL. Never raises.

    Returns the HTTP status, or None when there was nothing to send to or
    the attempt did not produce one. The outcome is recorded on the
    workspace so "is my webhook working?" is answerable without a support
    request — the same reason alerts themselves are recorded even when
    delivery fails.
    """
    url = configured_url(workspace)
    if url is None:
        return None

    status: int | None = None
    try:
        # Re-validated here, not only at save time: this is the check that
        # actually protects the request being made right now.
        validate(url)
        response = await _post(url, payload)
        status = response.status_code
    except WebhookRefused as exc:
        logger.warning("workspace %s: webhook refused at send time: %s", workspace.id, exc)
    except httpx.HTTPError as exc:
        logger.info("workspace %s: webhook not delivered: %s", workspace.id, type(exc).__name__)

    workspace.webhook_last_status = status
    workspace.webhook_last_attempt_at = utcnow()
    db.commit()
    return status


async def _post(url: str, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        # The single most important argument in this module.
        follow_redirects=False,
    ) as client:
        return await client.post(url, json=payload, headers={"User-Agent": "link-intelligence-webhook"})


def payload_for(*, alert_type: str, title: str, body: str) -> dict[str, Any]:
    """What goes over the wire, and deliberately nothing else.

    No email, no user id, no workspace id, no key of any kind: the
    receiving end is a third party the platform knows nothing about, so it
    is told the alert and not who it belongs to.
    """
    return {
        "type": alert_type,
        "title": title,
        "body": body,
        "sent_at": utcnow().isoformat(),
        "source": "link-intelligence",
    }
