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

from app import metrics
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, get_session_user
from app.models import User, WorkflowRun
from app.schemas import SystemStatus, WorkflowRunOut, WorkflowRunReport

router = APIRouter(prefix="/status", tags=["status"])


def _schema_version(db: Session) -> str | None:
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        # A database built by create_all rather than alembic. A normal
        # test/dev shape, not an outage.
        return None


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
        **metrics.snapshot(),
    )


@router.post("/workflow-runs", response_model=WorkflowRunOut, status_code=status.HTTP_201_CREATED)
def report_workflow_run(
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
    return run
