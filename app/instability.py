"""Idea 163: noticing when a domain keeps getting corrected.

A classifier that is wrong once is a classifier. A classifier that is
wrong about the *same site* over and over is a rule that does not fit,
and the only party who can see that pattern is the platform — the person
doing the correcting sees one link at a time.

**What counts as the signal, and what does not.** Every correction here
is a human one (``ClassificationFeedback`` is only written when somebody
changes a category by hand), so there is no risk of the system alerting
about its own automatic revisions. Corrections are counted per domain per
workspace: one tenant's habits say nothing about another's, and merging
them would leak the shape of one workspace's data into another's alert.

**Why it fires once, at the crossing.** The alert is raised when the
count reaches the threshold exactly, not whenever it is at or above it.
A domain corrected twenty times would otherwise send seventeen identical
messages, and an alert that repeats is an alert that gets muted — which
costs the next, different alert its audience too.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts import UNSTABLE_CATEGORY
from app.models import ClassificationFeedback, Notification
from app.notify import raise_alert

# Two corrections on one domain is a coincidence — a person changing their
# mind, or two genuinely different pages on one site. Three is a pattern
# worth a message. Deliberately small: the cost of being told about a
# stable domain is one message, the cost of never being told is a rule
# that stays wrong.
UNSTABLE_THRESHOLD = 3


def correction_count(db: Session, workspace_id: int, domain: str) -> int:
    return (
        db.execute(
            select(func.count())
            .select_from(ClassificationFeedback)
            .where(
                ClassificationFeedback.workspace_id == workspace_id,
                ClassificationFeedback.domain == domain,
            )
        ).scalar()
        or 0
    )


def category_spread(db: Session, workspace_id: int, domain: str) -> list[tuple[str, int]]:
    """Which categories this domain has been corrected *to*, commonest first.

    Reported in the alert rather than reduced to a single number because
    the two shapes it can take need different fixes: one target category
    means the rules are consistently wrong and a rule would fix it; several
    means the domain genuinely hosts mixed content and no rule will.
    """
    rows = db.execute(
        select(ClassificationFeedback.new_category, func.count())
        .where(
            ClassificationFeedback.workspace_id == workspace_id,
            ClassificationFeedback.domain == domain,
        )
        .group_by(ClassificationFeedback.new_category)
        .order_by(func.count().desc())
    ).all()
    return [(category, count) for category, count in rows]


async def report_if_unstable(db: Session, workspace_id: int, domain: str) -> Notification | None:
    """Raise the alert if this correction is the one that crosses the line.

    Call after the correction has been committed. Returns None when the
    threshold was not reached, was already passed, or the alert type is
    switched off.
    """
    if not domain:
        return None

    count = correction_count(db, workspace_id, domain)
    if count != UNSTABLE_THRESHOLD:
        return None

    spread = category_spread(db, workspace_id, domain)
    verdict = (
        "كل التصحيحات إلى تصنيف واحد — قاعدة مفقودة لا محتوى مختلط."
        if len(spread) == 1
        else "التصحيحات موزّعة على أكثر من تصنيف — النطاق نفسه مختلط المحتوى."
    )
    body = "\n".join(
        [
            f"صُحِّح تصنيف روابط من «{domain}» {count} مرّات.",
            "",
            "إلى أين صُحِّحت:",
            *(f"  • {category} — {times}" for category, times in spread),
            "",
            verdict,
        ]
    )
    return await raise_alert(db, workspace_id, UNSTABLE_CATEGORY.key, title="🧭 تصنيف غير مستقرّ", body=body)
