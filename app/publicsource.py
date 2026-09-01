"""Reading a public Telegram channel with no account at all.

Telegram publishes a read-only web preview for any channel that has a
public username, at ``https://t.me/s/<username>``. It is ordinary HTML
served to anonymous visitors — no login, no MTProto, no session string —
so reading it costs none of the ten userbot slots and puts none of them at
risk of a ban.

**What this is not.** It is not a second way to do what the userbot does,
and the difference decides where each is used:

* Channels only. A group has no ``/s/`` preview, so a public *group* link
  routed here would fetch a page that exists but lists nothing, and the
  operator would see "0 links" rather than "wrong tool". The router below
  refuses to guess.
* Public only. A private channel has no preview by definition.
* Whatever the channel chose to publish, which is not necessarily its full
  history, and never its comments.
* Polled, not pushed. There is no live path here.

So its real value is narrow and worth stating exactly: it reads a public
channel **the accounts are not members of**, without joining it. That is
precisely the case the operator asked about — "put the link in the web and
read it without the userbot" — and it is the only case it answers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

PUBLIC_PREVIEW = "https://t.me/s/{username}"

# The hosts that address a Telegram resource. Kept separate from
# app.classifier.platform's table: that one answers "what is this link
# about", this one answers "can I route it", and merging them would mean a
# new platform entry silently changing routing behaviour.
_TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})

# A public username: 5-32 characters, letters/digits/underscore. Telegram's
# own rule also forbids a leading digit and a trailing underscore, and both
# are worth enforcing here rather than discovering after a wasted fetch.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]$")

# The two shapes an invite link takes. Both mean the same thing — a chat
# that cannot be read without joining it — and both must route away from
# the scraper.
_INVITE_PREFIXES = ("+", "joinchat/")

# Reserved paths that look like usernames and are not.
_RESERVED = frozenset({"s", "c", "share", "addstickers", "proxy", "socks", "iv", "login", "joinchat"})


@dataclass(frozen=True)
class SourceRef:
    """What an operator-supplied link or id turned out to be.

    ``kind`` is one of:

    ``public``
        A public channel username. Readable by the scraper, no account
        needed.
    ``invite``
        An invite link. Requires a userbot to join before anything can be
        read — and joining is the single most ban-prone action a userbot
        performs, so this never happens automatically.
    ``id``
        A bare numeric chat id. Only meaningful to an account that is
        already in the chat; nothing can be done with it otherwise.
    """

    kind: str
    value: str


def classify_source(raw: str) -> SourceRef | None:
    """Route one operator-supplied link or id. ``None`` if unusable.

    Returning ``None`` rather than guessing is the whole point. The
    tempting version of this function treats anything unrecognised as a
    username and lets the fetch decide — which turns a typo into a request
    to Telegram and an empty result into "this channel has no links".
    """
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("@"):
        return _as_public(text[1:])

    if text.lstrip("-").isdigit():
        return SourceRef("id", text)

    if "//" not in text and "." in text.split("/", 1)[0]:
        text = "https://" + text  # bare "t.me/foo"

    try:
        parsed = urlparse(text)
    except ValueError:
        return None

    host = parsed.netloc.lower().partition(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if host not in _TELEGRAM_HOSTS:
        # Not a Telegram address at all. A username with no host is still
        # usable; anything else is not ours to route.
        return _as_public(text) if "/" not in text else None

    path = unquote(parsed.path).strip("/")
    if not path:
        return None

    if path.startswith(_INVITE_PREFIXES):
        return SourceRef("invite", text)

    # t.me/s/<username> is the preview URL itself — accept it, since an
    # operator who found the channel that way will paste what they see.
    if path.startswith("s/"):
        path = path[2:]

    # t.me/c/<internal id>/... addresses a private chat by internal id.
    if path.startswith("c/"):
        return SourceRef("invite", text)

    first = path.split("/", 1)[0]
    return _as_public(first)


def _as_public(username: str) -> SourceRef | None:
    name = username.strip().strip("/")
    if name.lower() in _RESERVED or not _USERNAME_RE.match(name):
        return None
    return SourceRef("public", name)


# --- reading the preview --------------------------------------------------

# One post's wrapper carries its number in data-post="<channel>/<id>",
# which is the same id MTProto uses — so a scraped row's watermark means
# the same thing a userbot-read row's does.
_POST_RE = re.compile(r'data-post="[^"/]+/(\d+)"', re.IGNORECASE)

# Message bodies. Telegram wraps each in this class; links inside are
# ordinary anchors.
_BODY_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


@dataclass(frozen=True)
class PublicPost:
    message_id: int
    text: str


def parse_preview(html: str) -> list[PublicPost]:
    """Pull (message id, text) out of one preview page, oldest first.

    Anchors are unwrapped to their href rather than their label, because
    Telegram renders a link's *text* as the shortened display form
    ("example.com/a…") while the href holds the real target. Extracting
    the visible text would store a truncated URL that resolves to nothing.
    """
    ids = _POST_RE.findall(html)
    bodies = _BODY_RE.findall(html)

    # A page whose post count and body count disagree is a page whose
    # structure changed. Pairing them positionally anyway would attach
    # links to the wrong message ids and poison the watermark, so the
    # mismatch is reported and the page is skipped.
    if len(ids) != len(bodies):
        logger.warning("preview layout mismatch: %d post ids, %d bodies — page skipped", len(ids), len(bodies))
        return []

    posts = []
    for raw_id, body in zip(ids, bodies, strict=True):
        posts.append(PublicPost(message_id=int(raw_id), text=_text_of(body)))
    return sorted(posts, key=lambda p: p.message_id)


_HREF_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>', re.IGNORECASE)


def _text_of(body: str) -> str:
    # Put every href into the text before tags are stripped, so a link that
    # only ever appears as an attribute is still collected.
    hrefs = [unescape(h) for h in _HREF_RE.findall(body)]
    text = _BR_RE.sub("\n", body)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)

    lines = [line.strip() for line in text.splitlines()]
    lines.extend(hrefs)
    return "\n".join(line for line in lines if line)[:4000]


# --- fetching and storing --------------------------------------------------

# Telegram serves the preview to anonymous visitors, but it is still
# Telegram's infrastructure and it still rate-limits by IP. "No account to
# ban" is not "no limit to hit", and a scraper that ignored that would take
# the deployment's own address out of service instead of an account.
FETCH_TIMEOUT_SECONDS = 20.0
MAX_PAGES_PER_RUN = 5

# Sent so the request is honest about what it is rather than pretending to
# be a browser. A scraper that lies about its identity is a scraper whose
# operator cannot answer for it.
USER_AGENT = "link-intelligence-platform/1.0 (+public channel preview reader)"


async def fetch_preview(username: str, *, before: int | None = None) -> str | None:
    """One preview page, or ``None`` if it could not be read.

    Never raises. A source that 404s (renamed, deleted, made private) must
    not end a run that has other sources to read, and it is a normal thing
    for a public channel to stop being public.
    """
    import httpx

    url = PUBLIC_PREVIEW.format(username=username)
    params = {"before": str(before)} if before else None
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # noqa: BLE001 - one unreachable source must not end the run
        logger.warning("public source %s unreachable: %s", username, exc)
        return None

    if response.status_code != 200:
        logger.warning("public source %s returned HTTP %s", username, response.status_code)
        return None
    return response.text


async def collect_public_channel(db, channel, *, fetch=fetch_preview) -> int:
    """Read one public channel's preview and store the links it holds.

    ``fetch`` is a parameter so the tests drive real parsing and real
    storage against fixture HTML without a network call — the seam is the
    HTTP boundary, which is the only part that cannot be exercised offline.

    Walks backwards from the newest page while pages still contain posts
    above the stored watermark, bounded by ``MAX_PAGES_PER_RUN`` so one
    very active channel cannot consume a whole run.
    """
    from app.ingest import ingest_text
    from app.timeutil import utcnow

    watermark = channel.last_message_id or 0
    highest = watermark
    stored = 0
    before: int | None = None

    for _ in range(MAX_PAGES_PER_RUN):
        html = await fetch(channel.username, before=before)
        if html is None:
            break

        posts = [p for p in parse_preview(html) if p.message_id > watermark]
        if not posts:
            break

        for post in posts:
            summary = ingest_text(
                db,
                workspace_id=channel.workspace_id,
                channel_id=channel.id,
                text=post.text,
                message_id=post.message_id,
            )
            stored += summary.stored
            highest = max(highest, post.message_id)

        # Page backwards from the oldest post on this page. When the oldest
        # is already at or below the watermark there is nothing older worth
        # asking for, so the walk stops rather than paging to the beginning
        # of the channel's history on every single run.
        oldest = min(p.message_id for p in posts)
        if oldest <= watermark + 1:
            break
        before = oldest

    channel.last_message_id = max(highest, watermark)
    channel.last_collected_at = utcnow()
    db.commit()
    return stored
