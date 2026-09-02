"""Running a candidate classifier beside the live one, changing nothing.

The reason this comes *before* a labelled benchmark, not after: a
benchmark needs a human to label a random sample, which is slow and
expensive, and most of that labour is spent confirming rows both
classifiers already agree on. Shadow mode narrows the work first —

    10,000 links → old rules → new rules → 420 disagreements

— and 420 rows a person can actually read is a benchmark that gets built.
10,000 is one that stays a plan.

Two properties make this safe to run against production data:

**It never writes.** No category is updated, no row is touched, nothing is
queued. The only output is a report. ``test_shadow_mode_never_writes``
pins it, because "shadow" that mutates is not shadow, it is a deployment.

**It re-derives, it does not re-collect.** ``classify_link`` is a pure
function of the URL and the stored context, both already in the database,
so comparing two rule sets across the whole corpus costs no Telegram
requests at all — which is also what makes a future re-classification
cheap (§45.6).

The default candidate is *the current engine*, and that is not a trivial
case: it answers "would today's rules still produce what is stored?" —
which finds rows classified by an older version, rows whose rule was since
changed, and rows a migration missed. Drift, in other words, measured
rather than assumed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.classifier import HUMAN_VERDICT, classify_link, may_reclassify
from app.models import Channel, Link

#: A candidate takes what the live classifier takes and returns a category.
Candidate = Callable[[str, str | None, str | None], str]

#: How many disagreeing rows are returned with the report. The point is a
#: list a person can read, not a second corpus to wade through.
SAMPLE_LIMIT = 50


@dataclass
class Disagreement:
    link_id: int
    url: str
    stored: str
    candidate: str
    stored_by: str


@dataclass
class ShadowReport:
    """What a candidate would change, and nothing changed."""

    compared: int = 0
    agreed: int = 0
    disagreed: int = 0
    #: Rows a human corrected, excluded from the comparison entirely: a
    #: candidate is not allowed an opinion about a verdict it may not
    #: overwrite (§44.3), and counting those as disagreements would make
    #: every rule change look worse the more a person had curated.
    human_verdicts_skipped: int = 0
    #: ``("movies_series", "books_courses") -> 37`` — which way categories
    #: move. The single number "420 disagreements" says a change is big;
    #: this says what it *does*.
    transitions: Counter[tuple[str, str]] = field(default_factory=Counter)
    samples: list[Disagreement] = field(default_factory=list)

    @property
    def disagreement_rate(self) -> float | None:
        """None, not zero, when nothing was compared — an empty corpus has
        no agreement rate, and 0% would read as perfect agreement."""
        if self.compared == 0:
            return None
        return round(self.disagreed / self.compared, 4)

    @property
    def biggest_transitions(self) -> list[tuple[tuple[str, str], int]]:
        return self.transitions.most_common(10)


def _default_candidate(url: str, context: str | None, channel_title: str | None) -> str:
    return classify_link(url, context, channel_title=channel_title).category


def compare(
    db: Session,
    workspace_id: int,
    *,
    candidate: Candidate | None = None,
    limit: int | None = None,
) -> ShadowReport:
    """Run ``candidate`` over stored links and report what would change.

    Reads only. ``limit`` bounds the scan for a large corpus on a small
    instance; ``None`` compares everything.
    """
    decide = candidate or _default_candidate
    report = ShadowReport()

    # Indexed per row rather than handed to ``dict()``: a SQLAlchemy Row
    # only *behaves* like a 2-tuple, and the shorter form needs a
    # ``type: ignore`` whose necessity varies by stub version — the same
    # trade already spelled out in app/routers/channels.py.
    titles: dict[int, str | None] = {
        row[0]: row[1]
        for row in db.query(Channel.id, Channel.title).filter(Channel.workspace_id == workspace_id).all()
    }

    query = (
        db.query(Link.id, Link.url, Link.raw_text, Link.category, Link.classified_by, Link.channel_id)
        .filter(Link.workspace_id == workspace_id)
        .order_by(Link.id)
    )
    if limit is not None:
        query = query.limit(limit)

    for link_id, url, raw_text, stored_category, stored_by, channel_id in query.all():
        if not may_reclassify(stored_by):
            report.human_verdicts_skipped += 1
            continue

        report.compared += 1
        proposed = decide(url or "", raw_text, titles.get(channel_id))
        if proposed == stored_category:
            report.agreed += 1
            continue

        report.disagreed += 1
        report.transitions[(stored_category, proposed)] += 1
        if len(report.samples) < SAMPLE_LIMIT:
            report.samples.append(
                Disagreement(
                    link_id=link_id,
                    url=url,
                    stored=stored_category,
                    candidate=proposed,
                    stored_by=stored_by or "",
                )
            )

    return report


__all__ = ["Candidate", "Disagreement", "ShadowReport", "HUMAN_VERDICT", "compare"]
