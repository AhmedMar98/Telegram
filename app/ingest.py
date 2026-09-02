"""Shared link ingestion: classify, deduplicate, store.

Every path that puts a link into the system goes through here — the
scheduled Telegram collector, the Telegram Desktop export importer, and
manual entry from the web UI — so classification, dedup and the savepoint
discipline are defined once instead of drifting per entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.classifier import (
    ADULT_CATEGORY,
    CLASSIFIER_VERSION,
    ClassificationResult,
    classify_link,
    detect_language,
    extract_url_spans,
    hash_url,
    link_platform,
    split_context,
)
from app.classifier.evidence import SIBLING_ASKER_MAX, SIBLING_DONOR_MIN
from app.dialogs import IMPORT_CHANNEL_PREFIX, MANUAL_CHANNEL_ID
from app.leads import detect as detect_lead
from app.models import Channel, Link, Message

# Re-exported from app.dialogs, which owns the definition and the
# ``is_synthetic`` test the assignment engine needs. Kept importable from
# here because both importers already say ``from app.ingest import
# IMPORT_CHANNEL_PREFIX``, and one definition with two names beats two
# definitions that can drift.
__all__ = ["IMPORT_CHANNEL_PREFIX", "MANUAL_CHANNEL_ID", "IngestSummary", "ingest_text", "store_link"]

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
    # Messages skipped because a ``messages`` row said they were already
    # processed. Counted, not silent: "collected nothing" and "had already
    # collected everything" are the same picture without this number.
    already_processed: int = 0
    # Messages that hit MAX_LINKS_PER_MESSAGE, and how many links that cost.
    # Counted rather than logged so a caller can report the loss instead of
    # discovering it only by noticing links that never arrived.
    truncated_messages: int = 0
    dropped_links: int = 0
    # Idea 152: newly stored links the classifier put in the adult
    # category. Collected here rather than alerted on at the point of
    # classification for one reason — store_link runs inside a SAVEPOINT
    # that a duplicate rolls back, so an alert sent there could announce a
    # link that was never stored. The caller sends after it commits.
    adult_urls: list[str] = field(default_factory=list)

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


def message_seen(db: Session, *, channel_id: int, message_id: int) -> bool:
    """Whether this exact Telegram message has already been fully processed.

    **Only real Telegram messages have identity here.** Manual entry and
    every importer use ``message_id = 0`` for every row they create, so
    treating 0 as an identity would make the second link a person adds by
    hand "a message already processed" and drop it without a word. The
    guard is the whole reason this is a function rather than a query
    inlined at the call site.
    """
    if message_id <= 0:
        return False
    return (
        db.query(Message.id).filter(Message.channel_id == channel_id, Message.tg_message_id == message_id).first()
        is not None
    )


def record_message(
    db: Session,
    *,
    workspace_id: int,
    channel_id: int,
    message_id: int,
    posted_at: datetime | None = None,
    sender_id: str | None = None,
    sender_username: str | None = None,
    sender_name: str | None = None,
) -> Message | None:
    """Mark a real Telegram message as processed. Returns None for id 0.

    Inside a SAVEPOINT for the same reason ``store_link`` is: two readers
    (the live listener and the scheduled collector) can reach the same
    message concurrently, and the loser of that race must lose one row, not
    the whole batch its transaction was carrying.
    """
    if message_id <= 0:
        return None
    row = Message(
        workspace_id=workspace_id,
        channel_id=channel_id,
        tg_message_id=message_id,
        sender_id=sender_id[:64] if sender_id else None,
        sender_username=sender_username[:200] if sender_username else None,
        sender_name=sender_name[:300] if sender_name else None,
        posted_at=posted_at,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return (
            db.query(Message).filter(Message.channel_id == channel_id, Message.tg_message_id == message_id).first()
        )
    return row


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
    channel_title: str | None = None,
    siblings: tuple[str, ...] = (),
    message_ref_id: int | None = None,
) -> ClassificationResult | None:
    """Classify and insert one link. Returns the classification, or None if
    the link was already present.

    Returning the result rather than a bare boolean is what lets the caller
    act on *what* was stored — idea 152 needs to know a new link landed in
    the adult category, and re-classifying it afterwards to find out would
    be both wasteful and capable of disagreeing with the stored row.

    The insert runs inside a SAVEPOINT so a duplicate rolls back only this
    row. A plain rollback would discard every uncommitted link collected
    alongside it, turning a routine repeat into silent data loss.
    """
    text = raw_text or ""
    result = classify_link(url, text, channel_title=channel_title, siblings=siblings)
    row = Link(
        workspace_id=workspace_id,
        channel_id=channel_id,
        message_id=message_id,
        message_ref_id=message_ref_id,
        url=url,
        url_hash=hash_url(url),
        domain=domain_of(url),
        platform=link_platform(url),
        category=result.category,
        confidence=result.confidence,
        classified_by=CLASSIFIER_VERSION,
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
        return None
    return result


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
    sender_id: str | None = None,
    sender_username: str | None = None,
    sender_name: str | None = None,
    channel_title: str | None = None,
    keyword_rules: list | None = None,
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

    # Message identity, before any work. Two readers exist — the hourly
    # collector and the live listener — and they overlap by design: the
    # listener catches a message the moment it arrives, and the collector
    # sweeps from the watermark to catch what the listener missed while the
    # free instance was asleep. Without this check the overlap re-ran lead
    # detection and re-classified every link on every pass.
    if message_seen(db, channel_id=channel_id, message_id=message_id):
        summary.already_processed += 1
        return summary

    # Lead detection hangs here rather than in the collector and the live
    # listener separately, for the reason that decides most placement
    # questions in this codebase: both of them already call this function
    # for *every* message — including the ones with no links at all, which
    # is most help requests — so one hook covers both and a third entry
    # point cannot forget it.
    #
    # Before the URL work below, so a message that matches the keywords is
    # recorded even if extracting its links then fails.
    detect_lead(
        db,
        workspace_id=workspace_id,
        channel_id=channel_id,
        text=text,
        message_id=message_id,
        sender_id=sender_id,
        sender_username=sender_username,
        sender_name=sender_name,
        rules=keyword_rules,
    )

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

    if not pairs:
        # Nothing durable came out of this message, so no ``messages`` row
        # is written: see the model's docstring for why a row means
        # "produced something", not "was looked at". Lead detection above
        # has already had its turn, and a message with neither a link nor a
        # match is cheap to re-read.
        return summary

    first_pass = {url: classify_link(url, context, channel_title=channel_title) for url, context, _ in pairs}
    siblings = _sibling_categories(first_pass)
    message = record_message(
        db,
        workspace_id=workspace_id,
        channel_id=channel_id,
        message_id=message_id,
        posted_at=posted_at,
        sender_id=sender_id,
        sender_username=sender_username,
        sender_name=sender_name,
    )

    for url, context, kind in pairs:
        stored = store_link(
            db,
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_id=message_id,
            url=url,
            raw_text=context,
            posted_at=posted_at,
            source_type=kind,
            forwarded_from=forwarded_from,
            channel_title=channel_title,
            # Only a link that could not decide for itself asks its
            # siblings. A link with its own strong evidence keeps it, so
            # sibling evidence can never overturn a confident answer — it
            # only fills in a blank one.
            siblings=siblings if first_pass[url].confidence <= SIBLING_ASKER_MAX else (),
            message_ref_id=message.id if message is not None else None,
        )
        if stored is None:
            summary.duplicates += 1
            continue
        summary.stored += 1
        if stored.category == ADULT_CATEGORY:
            summary.adult_urls.append(url)
    return summary


def _sibling_categories(first_pass: dict[str, ClassificationResult]) -> tuple[str, ...]:
    """The category the *confident* links in one message agree on, if any.

    A message that lists five films labels some of them and leaves the rest
    as bare URLs. The labelled ones are evidence about the bare ones — and
    that is the only way a link with nothing written around it gets a
    category at all.

    Three guards keep this from becoming a rumour mill:

    - Only links confident on their **own** evidence may lend
      (``SIBLING_DONOR_MIN``), and ``first_pass`` is computed with no
      sibling evidence at all, so nothing can lend a category it was lent.
    - Only **unanimity** counts. A message mixing films and books would
      otherwise hand both categories to every undecided link in it, which
      is evidence for nothing.
    - A single-link message has no siblings by definition.
    """
    if len(first_pass) < 2:
        return ()
    donors = {result.category for result in first_pass.values() if result.confidence >= SIBLING_DONOR_MIN}
    return (donors.pop(),) if len(donors) == 1 else ()
