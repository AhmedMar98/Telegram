"""Noticing that somebody asked for something.

The link collector answers "what was shared?". This answers a different
question over the same message stream: "who needs something we do?" — the
second product the platform was asked for, built on the first's pipeline
rather than beside it.

**Off unless ``LEADS_ENABLED`` is set.** Not a convenience flag. Everything
stored until now was a link; this stores identifiable third parties who
never opted in, and a deployment should say yes to that rather than find
out later. With the flag unset ``detect`` returns immediately, no table is
touched, and the dashboard says the feature is off.

No network and no model. Weighted phrase matching in-process, for the same
reason the link classifier's first tier is rules: it costs nothing, it
cannot rate-limit, it cannot be down, and it can be explained to the person
looking at a false positive — which a score from a language model cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.arabic import normalise
from app.config import get_settings
from app.models import Beneficiary, KeywordRule, Lead, Message
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

LEAD_STATUSES = ("new", "contacted", "converted", "ignored")

# How much of the message is kept. Long enough to judge a request without
# opening Telegram, short enough that this is not a message archive.
MAX_LEAD_TEXT = 1000


def leads_enabled() -> bool:
    return bool(get_settings().leads_enabled)


# Arabic normalisation lives in app/arabic.py: the classifier folds message
# text exactly the same way when weighing keywords as evidence, and two
# foldings that drift apart would make one message match a lead rule and
# not a category rule for reasons nobody could see.


@dataclass
class Match:
    """What the rules made of one message."""

    score: int = 0
    phrases: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.score > 0


def score_text(text: str, rules: list[KeywordRule]) -> Match:
    """Sum the weights of every active phrase present in the message.

    Substring matching over normalised text rather than word matching, and
    that is the right call for Arabic: prefixes ("المشروع") and suffixes
    ("مشروعي") attach to the word itself, so a word-boundary match would
    miss the majority of real phrasings.
    """
    haystack = normalise(text)
    if not haystack:
        return Match()

    result = Match()
    for rule in rules:
        if not rule.is_active:
            continue
        needle = normalise(rule.phrase)
        if needle and needle in haystack:
            result.score += rule.weight
            result.phrases.append(rule.phrase)
    return result


def active_rules(db: Session, workspace_id: int) -> list[KeywordRule]:
    return (
        db.query(KeywordRule)
        .filter(KeywordRule.workspace_id == workspace_id, KeywordRule.is_active.is_(True))
        .all()
    )


def upsert_beneficiary(
    db: Session,
    workspace_id: int,
    *,
    tg_user_id: str | None,
    username: str | None,
    display_name: str | None,
) -> Beneficiary | None:
    """Find or create the person behind a request.

    Returns None when the sender is unknown, which is a real case rather
    than an error: a channel post has no author, and a lead from one is
    still worth recording — it just has nobody to contact.

    Username and display name are refreshed on every sighting. They change,
    and a stale @handle is worse than none: it addresses somebody else.
    """
    if not tg_user_id:
        return None

    person = (
        db.query(Beneficiary)
        .filter(Beneficiary.workspace_id == workspace_id, Beneficiary.tg_user_id == str(tg_user_id))
        .first()
    )
    now = utcnow()
    if person is None:
        person = Beneficiary(
            workspace_id=workspace_id,
            tg_user_id=str(tg_user_id),
            username=(username or None),
            display_name=display_name[:300] if display_name else None,
            first_seen_at=now,
            last_seen_at=now,
            request_count=0,
        )
        db.add(person)
        db.flush()
    else:
        if username:
            person.username = username
        if display_name:
            person.display_name = display_name[:300]
        person.last_seen_at = now

    person.request_count = (person.request_count or 0) + 1
    return person


def detect(
    db: Session,
    *,
    workspace_id: int,
    channel_id: int,
    text: str,
    message_id: int,
    sender_id: str | None = None,
    sender_username: str | None = None,
    sender_name: str | None = None,
    rules: list[KeywordRule] | None = None,
) -> Lead | None:
    """Record a lead if this message matches. Never raises.

    Never raises for the same reason the link path does not: this runs
    inside ingestion, and a failure here must cost the message's links, not
    the whole run. A lead missed is a lead; a run lost is every link in it.

    ``rules`` may be passed in by a caller processing many messages, so the
    rule set is read once per batch rather than once per message.
    """
    if not leads_enabled() or not text:
        return None

    try:
        rule_set = active_rules(db, workspace_id) if rules is None else rules
        if not rule_set:
            return None

        match = score_text(text, rule_set)
        if not match.matched:
            return None

        # Seen twice in normal operation: the live listener catches the
        # message as it arrives and the hourly collector reads it again out
        # of history. Checked rather than left to the unique constraint so
        # the common case is a SELECT, not a caught IntegrityError.
        existing = (
            db.query(Lead)
            .filter(
                Lead.workspace_id == workspace_id,
                Lead.channel_id == channel_id,
                Lead.message_id == message_id,
            )
            .first()
        )
        if existing is not None:
            return existing

        person = upsert_beneficiary(
            db,
            workspace_id,
            tg_user_id=sender_id,
            username=sender_username,
            display_name=sender_name,
        )

        lead = Lead(
            workspace_id=workspace_id,
            beneficiary_id=person.id if person is not None else None,
            channel_id=channel_id,
            message_id=message_id,
            text=text[:MAX_LEAD_TEXT],
            matched=", ".join(match.phrases)[:300],
            score=match.score,
            status="new",
        )
        db.add(lead)
        db.flush()
        return lead
    except Exception as exc:  # noqa: BLE001 - a missed lead must not cost the message's links
        logger.warning("lead detection failed for message %s: %s", message_id, exc)
        return None


# --- retention -------------------------------------------------------------


def purge_expired(db: Session, workspace_id: int | None = None) -> int:
    """Delete leads past the retention window. Returns how many went.

    Retention is not optional housekeeping here. This table holds other
    people's words, and keeping them forever by default is the thing that
    turns a lead pipeline into an archive nobody agreed to. The beneficiary
    row survives with its counter, so "this person has asked four times"
    outlives the texts themselves.
    """
    days = get_settings().leads_retention_days
    if days <= 0:
        return 0

    cutoff = utcnow() - timedelta(days=days)
    query = db.query(Lead).filter(Lead.created_at < cutoff)
    if workspace_id is not None:
        query = query.filter(Lead.workspace_id == workspace_id)

    removed = query.delete(synchronize_session=False)
    db.commit()
    if removed:
        logger.info("purged %d lead(s) older than %d days", removed, days)
    return removed


def forget(db: Session, workspace_id: int, beneficiary_id: int) -> bool:
    """Erase one person and everything recorded about them.

    Deletes the leads too rather than orphaning them: a lead's text is that
    person's words, so "forget this person" that left their messages behind
    would be a deletion in name only.

    ``messages`` rows are **kept but stripped of attribution**. That row is
    the provenance of a *link* — the record that a URL came from a
    particular message in a particular channel — and deleting it would
    erase the origin of somebody else's data to satisfy this request. What
    it must not do is keep naming the person, so the sender columns are
    cleared. Their own words are already gone with the leads.
    """
    person = (
        db.query(Beneficiary)
        .filter(Beneficiary.workspace_id == workspace_id, Beneficiary.id == beneficiary_id)
        .first()
    )
    if person is None:
        return False

    db.query(Lead).filter(Lead.workspace_id == workspace_id, Lead.beneficiary_id == person.id).delete(
        synchronize_session=False
    )
    if person.tg_user_id:
        db.query(Message).filter(
            Message.workspace_id == workspace_id, Message.sender_id == person.tg_user_id
        ).update(
            {"sender_id": None, "sender_username": None, "sender_name": None},
            synchronize_session=False,
        )
    db.delete(person)
    db.commit()
    return True
