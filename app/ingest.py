"""Shared link ingestion: classify, deduplicate, store.

Every path that puts a link into the system goes through here — the
scheduled Telegram collector, the Telegram Desktop export importer, and
manual entry from the web UI — so classification, dedup and the savepoint
discipline are defined once instead of drifting per entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.classifier import classify_link, detect_language, extract_url_spans, hash_url, split_context
from app.models import Channel, Link

# Synthetic channel identifiers for links that did not arrive from a real
# Telegram channel. They are ordinary channel rows so that the
# (channel_id, url_hash) uniqueness constraint keeps working unchanged;
# making channel_id nullable instead would silently break dedup, because
# SQL treats NULLs as distinct from each other.
MANUAL_CHANNEL_ID = "manual"
IMPORT_CHANNEL_PREFIX = "import:"

# A single message carrying more than this many links is a dump, not a post.
# The first links in such a message are the ones a person actually wrote
# about; the tail is almost always a mirror list or a spam block, and storing
# all of it buries the useful links under noise. Capped rather than dropped
# entirely, because the leading links are usually still worth keeping.
MAX_LINKS_PER_MESSAGE = 50


@dataclass
class IngestSummary:
    stored: int = 0
    duplicates: int = 0
    scanned: int = 0
    # Messages that hit MAX_LINKS_PER_MESSAGE, and how many links that cost.
    # Counted rather than logged so a caller can report the loss instead of
    # discovering it only by noticing links that never arrived.
    truncated_messages: int = 0
    dropped_links: int = 0

    @property
    def total_found(self) -> int:
        return self.stored + self.duplicates


def domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return url[:300]
    return (netloc[4:] if netloc.startswith("www.") else netloc) or url[:300]


def get_or_create_channel(
    db: Session, *, workspace_id: int, tg_channel_id: str, title: str | None = None, username: str | None = None
) -> Channel:
    channel = (
        db.query(Channel)
        .filter(Channel.workspace_id == workspace_id, Channel.tg_channel_id == tg_channel_id)
        .first()
    )
    if channel is None:
        channel = Channel(workspace_id=workspace_id, tg_channel_id=tg_channel_id, title=title, username=username)
        db.add(channel)
        db.flush()
    return channel


def manual_channel(db: Session, workspace_id: int) -> Channel:
    """The bucket for links a person added by hand."""
    return get_or_create_channel(
        db, workspace_id=workspace_id, tg_channel_id=MANUAL_CHANNEL_ID, title="إضافة يدوية"
    )


def store_link(
    db: Session,
    *,
    workspace_id: int,
    channel_id: int,
    message_id: int,
    url: str,
    raw_text: str | None = None,
    posted_at: datetime | None = None,
    source_type: str = "text",
    forwarded_from: str | None = None,
) -> bool:
    """Classify and insert one link. Returns True if it was new.

    The insert runs inside a SAVEPOINT so a duplicate rolls back only this
    row. A plain rollback would discard every uncommitted link collected
    alongside it, turning a routine repeat into silent data loss.
    """
    text = raw_text or ""
    result = classify_link(url, text)
    row = Link(
        workspace_id=workspace_id,
        channel_id=channel_id,
        message_id=message_id,
        url=url,
        url_hash=hash_url(url),
        domain=domain_of(url),
        category=result.category,
        confidence=result.confidence,
        classified_by="llm" if result.matched_rule.startswith("llm") else "rules",
        matched_rule=result.matched_rule[:100],
        source_type=source_type,
        forwarded_from=forwarded_from[:300] if forwarded_from else None,
        language=detect_language(text),
        raw_text=text[:2000] or None,
        posted_at=posted_at,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return False
    return True


def ingest_text(
    db: Session,
    *,
    workspace_id: int,
    channel_id: int,
    text: str,
    message_id: int = 0,
    posted_at: datetime | None = None,
    extra_urls: list[str] | None = None,
    button_urls: list[str] | None = None,
    forwarded_from: str | None = None,
    summary: IngestSummary | None = None,
) -> IngestSummary:
    """Pull every URL out of a blob of text and store each one.

    ``extra_urls`` carries links that are not present in the visible text —
    Telegram hyperlinks where the href hides behind a caption.
    ``button_urls`` carries inline-keyboard targets, which are not part of
    the message body at all. Both are kept as separate arguments rather than
    merged into one list because each records a different provenance on the
    stored row, and that distinction is the whole point of recording it.
    """
    summary = summary or IngestSummary()
    summary.scanned += 1

    spans = extract_url_spans(text)
    contexts = split_context(text, spans)
    pairs: list[tuple[str, str, str]] = [
        (url, context, "text") for (url, _, _), context in zip(spans, contexts, strict=True)
    ]

    # Off-body targets have no position in the visible text, so there is no
    # segment to attribute to them; they take the whole message as context.
    seen = {url for url, _, _ in pairs}
    for url, kind in [*[(u, "hyperlink") for u in extra_urls or []], *[(u, "button") for u in button_urls or []]]:
        if url not in seen:
            seen.add(url)
            pairs.append((url, text, kind))

    if len(pairs) > MAX_LINKS_PER_MESSAGE:
        summary.truncated_messages += 1
        summary.dropped_links += len(pairs) - MAX_LINKS_PER_MESSAGE
        pairs = pairs[:MAX_LINKS_PER_MESSAGE]

    for url, context, kind in pairs:
        if store_link(
            db,
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_id=message_id,
            url=url,
            raw_text=context,
            posted_at=posted_at,
            source_type=kind,
            forwarded_from=forwarded_from,
        ):
            summary.stored += 1
        else:
            summary.duplicates += 1
    return summary
