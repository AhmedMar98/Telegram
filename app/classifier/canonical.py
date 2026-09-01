"""One spelling of a URL, so two spellings of it stop becoming two rows.

The same link reaches this system written several ways. The same channel
post is forwarded with a tracking parameter appended; somebody pastes the
``www.`` form of a link the collector already stored without it; a share
button adds ``?utm_source=telegram``; a message ends the URL with a
trailing slash the previous one lacked. Every one of those is the same
resource, and every one of them used to produce a **separate row** —
because ``url_hash`` was computed over the raw string.

Two properties define this module, and both are load-bearing:

**It never rewrites the stored URL.** ``Link.url`` keeps exactly what the
message said; canonicalisation feeds ``hash_url`` alone. That containment
is what makes a wrong rule here survivable: the worst a bad rule can do is
merge two rows that were distinct, or fail to merge two that were the
same. It cannot corrupt the link a person clicks — which is what would
happen if the "cleaned" form were stored and the cleaning were wrong about
a query parameter the site actually needed.

**It never raises.** A URL that cannot be parsed is returned stripped and
otherwise untouched. Ingestion runs over whatever arbitrary text a channel
posted, and a classifier that throws on a malformed URL would take down a
collection run over one bad message.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that identify *where a click came from*, never *what is being
# linked to*. Removing them merges the shared copy of a link with the
# original; keeping them splits one resource into as many rows as there
# were sharers.
#
# Deliberately a short, named list rather than a broad pattern. ``ref`` and
# ``si`` are not here even though they are usually tracking, because on
# some sites they select content — and a parameter wrongly dropped merges
# two genuinely different pages into one row, which is the failure this
# module cannot detect afterwards.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref_src",
        "ref_url",
        "spm",
    }
)
_TRACKING_PREFIXES = ("utm_",)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonical_url(url: str) -> str:
    """The comparison form of ``url``. Pure, total, and never stored.

    Six rules, each of which merges spellings that name the same resource:

    1. Scheme and host are lower-cased — **and nothing else is**. Host and
       scheme are case-insensitive by RFC 3986; a path is not.
       ``/File.pdf`` and ``/file.pdf`` are two resources on any
       case-sensitive server, and the previous behaviour (lower-casing the
       whole URL before hashing) merged them.
    2. A leading ``www.`` is dropped, matching what ``domain_of`` already
       does for the stored ``domain`` column.
    3. A default port is dropped: ``:80`` on http, ``:443`` on https.
    4. The fragment is dropped — it is never sent to the server — **except**
       when it starts with ``!`` or ``/``, which are hashbang and hash-router
       forms where the fragment *is* the address of the content.
    5. Tracking parameters are dropped; the surviving parameters keep the
       order they were written in, because a server may treat repeated keys
       positionally.
    6. A trailing slash is dropped from a non-root path.
    """
    text = (url or "").strip()
    if not text:
        return ""

    try:
        parts = urlsplit(text)
    except ValueError:
        # A malformed URL is not a reason to lose the link or to fail a
        # collection run. It hashes as itself, which is exactly as
        # dedupe-able as it was before this module existed.
        return text

    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    # Credentials in the netloc are preserved: dropping them would merge
    # two URLs that authenticate as different users.
    if parts.username:
        credentials = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{credentials}@{netloc}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query = parts.query
    if query:
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if not _is_tracking(k)]
        query = urlencode(kept)

    fragment = parts.fragment if parts.fragment.startswith(("!", "/")) else ""

    return urlunsplit((scheme, netloc, path, query, fragment))
