"""The operator's screen: what is deployed, and is any of it failing.

Ideas 181, 183, 185, 187 and 192 all answer one question — "is the system
healthy right now, and which code is it running?" — so they are one
endpoint rather than five.

**The credential direction is inverted on purpose.** Ideas 181 and 183 ask
this service to *read* the GitHub Actions API, which would mean storing a
GitHub token with repo scope in a database whose entire security model has
been built around not holding anything that powerful. Instead the
workflows *report in* when they finish, authenticating with a personal API
key (phase 8a) that cannot touch the repository at all.

The trade is real and stated: a run that never starts reports nothing, so
an absent row means either healthy-and-idle or the workflow is not running
— the same ambiguity ``looks_stalled`` already resolves for the collector,
resolved the same way here, by age.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import coverage, live, metrics, shadow
from app.alerts import BACKUP_RESULT
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, get_session_user
from app.models import User, WorkflowRun
from app.notify import raise_alert
from app.schemas import (
    ClassificationDrift,
    CollectionCoverage,
    CoverageHistory,
    CoverageSnapshotOut,
    LiveStatus,
    SystemStatus,
    WorkflowRunOut,
    WorkflowRunReport,
)

router = APIRouter(prefix="/status", tags=["status"])


def _schema_version(db: Session) -> str | None:
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        # A database built by create_all rather than alembic. A normal
        # test/dev shape, not an outage.
        return None


@router.get("/coverage/history", response_model=CoverageHistory)
def coverage_history(
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> CoverageHistory:
    """The measurement over time — the question a snapshot cannot answer.

    99.2%, then 98.7%, then 94.1% is a system degrading in plain sight,
    and each of those readings looks acceptable on its own.
    """
    snapshots = coverage.history(db, current_user.workspace_id, limit=max(1, min(limit, 500)))
    return CoverageHistory(
        snapshots=[CoverageSnapshotOut.model_validate(row) for row in snapshots],
        trend=coverage.trend(snapshots),
    )


@router.get("/classification-drift", response_model=ClassificationDrift)
def classification_drift(
    limit: int = 5_000,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_session_user),
) -> ClassificationDrift:
    """Would today's rules still produce what is stored? (§47.3)

    Shadow mode against the live engine: it reads, compares and reports,
    and writes nothing at all. The disagreements are the shortlist worth
    a human's attention — which is how a labelled benchmark gets built
    without labelling the whole corpus.
    """
    report = shadow.compare(db, current_user.workspace_id, limit=max(1, min(limit, 50_000)))
    return ClassificationDrift(
        compared=report.compared,
        agreed=report.agreed,
        disagreed=report.disagreed,
        human_verdicts_skipped=report.human_verdicts_skipped,
        disagreement_rate=report.disagreement_rate,
        biggest_transitions={
            f"{before} -> {after}": count for (before, after), count in report.biggest_transitions
        },
        samples=[vars(sample) for sample in report.samples],
    )


@router.get("/coverage", response_model=CollectionCoverage)
def collection_coverage(
    db: Session = Depends(get_db), current_user: User = Depends(get_session_user)
) -> CollectionCoverage:
    """How much of what was due actually got collected (§46).

    A read-only computation over the columns the collector stamps; it
    never triggers a collection and never writes. Placed on the status
    router rather than under /channels because it describes the *system's*
    behaviour over the sources, not the sources themselves.
    """
    report = coverage.measure(db, current_user.workspace_id)
    return CollectionCoverage(
        sources_expected=report.sources_expected,
        sources_due=report.sources_due,
        sources_overdue=report.sources_overdue,
        sources_attempted=report.sources_attempted,
        sources_succeeded=report.sources_succeeded,
        sources_failed=report.sources_failed,
        sources_skipped=report.sources_skipped,
        failures_by_kind=report.failures_by_kind,
        coverage_rate=report.coverage_rate,
        failure_rate=report.failure_rate,
        gap_rate=report.gap_rate,
        collection_lag_seconds=report.collection_lag_seconds,
        watermark_lag_seconds=report.watermark_lag_seconds,
        is_fresh=report.is_fresh,
        duplicate_message_rate=report.duplicates.duplicate_message,
        duplicate_link_occurrence_rate=report.duplicates.duplicate_link_occurrence,
        duplicate_resource_rate=report.duplicates.duplicate_resource,
        watermark_regressions=report.watermark.regressions,
        watermark_behind=report.watermark.behind,
        watermark_ownership_conflicts=report.watermark.ownership_conflicts,
        watermark_sound=report.watermark.sound,
    )


@router.get("", response_model=SystemStatus)
def system_status(db: Session = Depends(get_db), current_user: User = Depends(get_session_user)) -> SystemStatus:
    """Deployment identity, live counters, and the newest run per workflow."""
    settings = get_settings()

    # One row per workflow name: the newest. Written as a correlated max
    # rather than fetching everything and filtering in Python, because the
    # table grows with every scheduled run forever.
    newest = (
        select(WorkflowRun.name, func.max(WorkflowRun.started_at).label("started_at"))
        .where(WorkflowRun.workspace_id == current_user.workspace_id)
        .group_by(WorkflowRun.name)
        .subquery()
    )
    rows = (
        db.query(WorkflowRun)
        .join(
            newest,
            (WorkflowRun.name == newest.c.name) & (WorkflowRun.started_at == newest.c.started_at),
        )
        .filter(WorkflowRun.workspace_id == current_user.workspace_id)
        .order_by(WorkflowRun.started_at.desc())
        .all()
    )

    return SystemStatus(
        deploy_commit=settings.render_git_commit,
        service_name=settings.render_service_name,
        schema_version=_schema_version(db),
        latest_runs=[WorkflowRunOut.model_validate(r) for r in rows],
        live=LiveStatus(**live.state().snapshot()),
        **metrics.snapshot(),
    )


# The workflow whose result is worth a message rather than only a row on
# the board (idea 158). Matched against the name the run reports, which is
# set in .github/workflows/backup.yml — and pinned by a test, because a
# rename there would otherwise silently stop the alert forever while
# everything still looked like it was working.
BACKUP_WORKFLOW_NAME = "backup"

SUCCESS_CONCLUSIONS = frozenset({"success"})


async def _confirm_backup(db: Session, workspace_id: int, run: WorkflowRun) -> None:
    """Idea 158: a weekly confirmation, not a weekly silence.

    Sent on **both** outcomes, which is the part that makes it useful. An
    alert that only fires on success cannot be told apart from an alert
    that stopped working: in both cases nothing arrives. Sending either way
    means the message's own absence is the signal — a week with no backup
    message means the workflow did not run at all.

    Only the backup workflow does this. Every other run lands on the status
    board and nowhere else: the collector already has a failure alert with
    far more context (idea 154), and a message per workflow per run would
    be several an hour, which is how a person learns to ignore all of them.
    """
    if run.name != BACKUP_WORKFLOW_NAME:
        return

    succeeded = run.conclusion in SUCCESS_CONCLUSIONS
    title = "💾 نسخة احتياطية ناجحة" if succeeded else "⛔ فشل النسخ الاحتياطي"
    body = (
        (
            "اكتملت النسخة الأسبوعية وحُفظت كأثر في GitHub Actions لمدة ٣٥ يوماً.\n"
            "هذه رسالة تأكيد دورية: غيابها في أسبوع ما يعني أنّ التشغيلة لم تعمل أصلاً."
        )
        if succeeded
        else (
            f"انتهت تشغيلة النسخ الاحتياطي بالنتيجة «{run.conclusion}».\n"
            "خطة Render المجانية تُنهي القاعدة بعد ٣٠ يوماً، والنسخة هي ما يجعل ذلك "
            "استعادةً لا فقداناً — راجع `docs/19-runbook.md` §النسخ الاحتياطي."
        )
    )
    await raise_alert(db, workspace_id, BACKUP_RESULT.key, title=title, body=body)


@router.post("/workflow-runs", response_model=WorkflowRunOut, status_code=status.HTTP_201_CREATED)
async def report_workflow_run(
    payload: WorkflowRunReport,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkflowRun:
    """A finished workflow reporting its own outcome.

    This is the one endpoint here that accepts an API key rather than a
    session, because the caller is a GitHub Actions job and not a browser
    — that is the whole point of the inversion. It writes only to this
    table, so a leaked key can at worst add noise to a status board;
    everything destructive still requires a session.
    """
    run = WorkflowRun(
        workspace_id=current_user.workspace_id,
        name=payload.name,
        conclusion=payload.conclusion,
        detail=payload.detail,
        commit_sha=payload.commit_sha,
        duration_seconds=payload.duration_seconds,
    )
    db.add(run)
    db.commit()
    await _confirm_backup(db, current_user.workspace_id, run)
    return run
