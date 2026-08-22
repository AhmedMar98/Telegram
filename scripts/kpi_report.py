"""Internal KPIs, computed from rows that actually exist.

    python scripts/kpi_report.py --workspace 1

Idea 248 asks for **honest** internal indicators, and names one it wants:
*"links actually retrieved through search, weekly"*.

**That metric cannot be computed in this system, and the first honest
thing this report does is say so.** Two reasons, both checkable:

1. **Searches are not recorded anywhere.** ``app/audit.py`` is called when
   something is added, recategorised or deleted — never when something is
   searched for. There is no table with a row per search.
2. **``Link.click_count`` is a running total with no time dimension.** It
   answers "how many times ever", never "how many times this week".

Neither is an oversight to fix in passing. Logging searches means storing
what people look for, which is a privacy decision and not a schema change;
and the project's own rule is that a metric is not invented to fill a
slot in a report.

So this reports the strongest *available* answer to the question behind
idea 248 — **is the archive actually used, or is it a write-only pile?** —
and labels precisely what each number does and does not cover.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    ApiKey,
    ClassificationFeedback,
    Link,
    Notification,
    Workspace,
)
from app.rls import scope_session_to_workspace  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

logger = logging.getLogger("kpi")

WINDOW_DAYS = 7


def _scalar(db, statement) -> int:
    return db.execute(statement).scalar() or 0


def collect(db, workspace_id: int) -> dict[str, object]:
    now = utcnow()
    since = now - timedelta(days=WINDOW_DAYS)
    mine = Link.workspace_id == workspace_id

    total = _scalar(db, select(func.count()).select_from(Link).where(mine))
    this_week = _scalar(db, select(func.count()).select_from(Link).where(mine, Link.created_at >= since))

    # The central number. A link that has never been opened is a link the
    # archive stored and nobody came back for; the share that *has* been
    # opened is the closest honest answer to "is this useful".
    opened_ever = _scalar(db, select(func.count()).select_from(Link).where(mine, Link.click_count > 0))
    opens_total = _scalar(db, select(func.coalesce(func.sum(Link.click_count), 0)).where(mine))

    corrections = _scalar(
        db,
        select(func.count())
        .select_from(ClassificationFeedback)
        .where(ClassificationFeedback.workspace_id == workspace_id),
    )
    corrections_week = _scalar(
        db,
        select(func.count())
        .select_from(ClassificationFeedback)
        .where(
            ClassificationFeedback.workspace_id == workspace_id,
            ClassificationFeedback.created_at >= since,
        ),
    )

    dead_week = _scalar(
        db,
        select(func.count())
        .select_from(Link)
        .where(mine, Link.is_alive.is_(False), Link.last_checked_at >= since),
    )
    never_checked = _scalar(db, select(func.count()).select_from(Link).where(mine, Link.last_checked_at.is_(None)))

    alerts = _scalar(
        db, select(func.count()).select_from(Notification).where(Notification.workspace_id == workspace_id)
    )
    alerts_undelivered = _scalar(
        db,
        select(func.count())
        .select_from(Notification)
        .where(Notification.workspace_id == workspace_id, Notification.delivered_count == 0),
    )

    keys_used = _scalar(
        db,
        select(func.count()).select_from(ApiKey).where(ApiKey.workspace_id == workspace_id, ApiKey.use_count > 0),
    )

    return {
        "total": total,
        "this_week": this_week,
        "opened_ever": opened_ever,
        "opens_total": opens_total,
        "reuse_share": (opened_ever / total * 100) if total else 0.0,
        "corrections": corrections,
        "corrections_week": corrections_week,
        "agreement": ((total - corrections) / total * 100) if total else 100.0,
        "dead_week": dead_week,
        "never_checked": never_checked,
        "alerts": alerts,
        "alerts_undelivered": alerts_undelivered,
        "keys_used": keys_used,
    }


def render(k: dict[str, object]) -> str:
    lines = [
        "# مؤشّرات الأداء الداخلية",
        "",
        "## هل يُستخدَم الأرشيف فعلاً؟",
        f"  الروابط المخزَّنة: {k['total']} ({k['this_week']} خلال {WINDOW_DAYS} أيام)",
        f"  فُتِح منها مرّة على الأقل: {k['opened_ever']} — أي {k['reuse_share']:.0f}٪",
        f"  إجمالي مرّات الفتح: {k['opens_total']}",
        "",
        "  ⚠ العدّاد **ناقص بالتصميم**: من ينسخ الرابط بدل الضغط عليه لا يُحصى.",
        "     وهو تراكميّ بلا زمن، فلا يُشتقّ منه رقم أسبوعي.",
        "",
        "## هل يُصيب التصنيف؟",
        f"  تصحيحات بشرية: {k['corrections']} ({k['corrections_week']} هذا الأسبوع)",
        f"  نسبة ما لم يُصحَّح: {k['agreement']:.1f}٪",
        "",
        "  ⚠ «لم يُصحَّح» ليست «صحيحة»: تصنيفٌ خاطئ لم يلحظه أحد يُحسَب هنا",
        "     كإصابة. الرقم حدّ أعلى لا قياس دقّة.",
        "",
        "## هل تتآكل المجموعة؟",
        f"  تأكّد موتها في فحوص هذا الأسبوع: {k['dead_week']}",
        f"  لم تُفحص بعد إطلاقاً: {k['never_checked']}",
        "",
        "## هل تصل التنبيهات؟",
        f"  تنبيهات سُجِّلت: {k['alerts']}، منها {k['alerts_undelivered']} لم تُسلَّم",
        "",
        "  الفجوة بين الرقمين هي «انتبه النظام» مقابل «أُبلِغتَ أنت».",
        "",
        "## الأتمتة",
        f"  مفاتيح API استُخدمت فعلاً: {k['keys_used']}",
        "",
        "## ما لا يستطيع هذا التقرير قوله",
        "",
        "  «كم رابطاً استُرجع عبر البحث هذا الأسبوع» — وهو ما طلبه البند ٢٤٨.",
        "  البحث غير مسجَّل في أيّ جدول، و`click_count` بلا بُعد زمني.",
        "  تسجيل عمليات البحث يعني تخزين ما يبحث عنه الناس: قرار خصوصية،",
        "  لا تعديل مخطّط. ولم يُتَّخذ.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=int, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = SessionLocal()
    try:
        if db.get(Workspace, args.workspace) is None:
            sys.exit(f"error: no workspace with id {args.workspace}")

        # notifications and classification_feedback are both under RLS;
        # unscoped, every count below would read a truthful-looking zero.
        scope_session_to_workspace(db, args.workspace)
        logger.info("%s", render(collect(db, args.workspace)))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
