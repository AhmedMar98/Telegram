#!/usr/bin/env python3
"""Is collection actually working? The one view that answers it.

    COLLECTOR_WORKSPACE_ID=1 python scripts/check_collection.py

Written because a process being alive says nothing about whether anything
is being collected, and until now there was no way to tell the difference
from outside. ``collection_runs``, ``source_progress`` and the findings in
``app.collection.health`` are the three things phase 3 added that make the
question answerable, and none of them was visible anywhere: not in the
API, not on the dashboard, not in a script. So the first real Telegram
test would have produced evidence nobody could read.

It reports and exits. Nothing here writes, migrates or repairs — the whole
point is a view you can run against production without thinking twice.

Four sections, in the order an operator actually asks them:

    accounts   can anything collect at all
    sources    is each source assigned, reachable, and moving
    runs       what did the last attempts actually do
    findings   what is wrong, named, with the row that proves it

**No score, and no "healthy" line.** A single verdict would average an
account whose session was revoked together with a channel that is simply
quiet, and the two need opposite responses. If there are no findings, the
findings section says so and nothing else claims anything.

Exit codes:
    0  no findings
    1  findings reported on stdout
    2  the environment is not usable
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.collection import health  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Channel,
    CollectionRun,
    SourceAccess,
    SourceAssignment,
    SourceProgress,
    TelegramAccount,
)
from app.rls import scope_session_to_workspace  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

RECENT_RUNS = 15


def _ago(value) -> str:
    """How long ago, or the honest absence.

    "never" and "0s ago" are different facts and a formatter that prints
    the second for the first is how a source that has never been touched
    reads as one that was touched a moment ago.
    """
    if value is None:
        return "never"
    delta: timedelta = utcnow() - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _accounts(db, workspace_id: int) -> None:
    rows = (
        db.execute(
            select(TelegramAccount)
            .where(TelegramAccount.workspace_id == workspace_id)
            .order_by(TelegramAccount.id)
        )
        .scalars()
        .all()
    )

    print("\n=== ACCOUNTS ===")
    if not rows:
        print("  none registered — nothing can collect")
        return
    for account in rows:
        held = (
            db.execute(
                select(SourceAssignment).where(
                    SourceAssignment.account_id == account.id,
                    SourceAssignment.released_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        print(
            f"  #{account.id} {account.label!r}"
            f"\n      state={account.state}"
            + (f" ({account.state_reason})" if account.state_reason else "")
            + f"\n      sources held={len(held)}"
            f"  consecutive failures={account.consecutive_failures}"
            f"\n      last success={_ago(account.last_success_at)}"
            f"  last failure={_ago(account.last_failure_at)}"
        )
        if account.last_error:
            print(f"      last error: {account.last_error[:120]}")
    # Stated rather than implied: nothing in the database knows whether a
    # Telegram socket is open right now. "Connected" is only ever inferred
    # from when something last worked, and saying so stops this report
    # being read as a liveness check it cannot perform.
    print("  (connection state is not stored; 'last success' is the closest fact there is)")


def _sources(db, workspace_id: int) -> None:
    rows = (
        db.execute(select(Channel).where(Channel.workspace_id == workspace_id).order_by(Channel.id))
        .scalars()
        .all()
    )

    print("\n=== SOURCES ===")
    if not rows:
        print("  none registered")
        return
    for channel in rows:
        assignment = db.execute(
            select(SourceAssignment).where(
                SourceAssignment.source_id == channel.id,
                SourceAssignment.released_at.is_(None),
            )
        ).scalar_one_or_none()
        live = db.execute(
            select(SourceProgress).where(
                SourceProgress.source_id == channel.id,
                SourceProgress.track == SourceProgress.LIVE,
            )
        ).scalar_one_or_none()
        historical = db.execute(
            select(SourceProgress).where(
                SourceProgress.source_id == channel.id,
                SourceProgress.track == SourceProgress.HISTORICAL,
            )
        ).scalar_one_or_none()
        access = db.execute(select(SourceAccess).where(SourceAccess.source_id == channel.id)).scalars().all()

        label = channel.username or channel.tg_channel_id
        print(f"  #{channel.id} {label}  ({channel.acquisition_method or 'acquisition unrecorded'})")
        print(f"      assigned to={'account ' + str(assignment.account_id) if assignment else 'NOBODY'}")
        if access:
            states = ", ".join(f"acct {a.account_id}:{a.state}" for a in access)
            print(f"      access={states}")
        else:
            print("      access=no observation recorded")
        if live is None:
            print("      live: no progress row — never attempted by the runtime")
        else:
            print(
                f"      live: watermark={live.current_watermark}"
                f"  attempt={_ago(live.last_attempt_at)}"
                f"  progress={_ago(live.last_progress_at)}"
                f"  coverage={live.coverage_status}"
            )
        if historical is not None:
            print(
                f"      historical: watermark={historical.current_watermark}"
                f"  attempt={_ago(historical.last_attempt_at)}"
                f"  progress={_ago(historical.last_progress_at)}"
            )
        # The legacy mirror, shown only when it disagrees. It should never
        # disagree; a line here means something wrote the column outside
        # app.progress, which is exactly what migration 0029's trigger
        # exists to make impossible.
        if live is not None and (channel.last_message_id or 0) != live.current_watermark:
            print(
                f"      !! legacy channels.last_message_id={channel.last_message_id} "
                f"disagrees with the live track ({live.current_watermark})"
            )


def _runs(db, workspace_id: int) -> None:
    rows = (
        db.execute(
            select(CollectionRun)
            .where(CollectionRun.workspace_id == workspace_id)
            .order_by(CollectionRun.id.desc())
            .limit(RECENT_RUNS)
        )
        .scalars()
        .all()
    )

    print(f"\n=== LAST {RECENT_RUNS} COLLECTION RUNS ===")
    if not rows:
        print("  none recorded.")
        print("  Note: only app/runtime/worker.py writes this table. If collection is")
        print("  running through the scheduled scripts/collect.py instead, this being")
        print("  empty is expected and is not evidence that nothing collected.")
        return
    for run in rows:
        window = ""
        if run.range_from or run.range_to:
            window = f"  range=[{run.range_from} .. {run.range_to}]"
        print(
            f"  run #{run.id} source={run.source_id} account={run.account_id}"
            f" path={run.acquisition_path} mode={run.mode}{window}"
        )
        print(
            f"      state={run.state}"
            f"  started={_ago(run.started_at)}"
            f"  finished={_ago(run.finished_at)}"
            f"  heartbeat={_ago(run.heartbeat_at)}"
        )
        print(
            f"      watermark {run.watermark_before} -> {run.watermark_after}"
            f"  seen={run.messages_seen} stored={run.links_stored}"
        )
        if run.failure_kind:
            print(f"      failure={run.failure_kind}: {(run.detail or '')[:120]}")


def _findings(db, workspace_id: int) -> int:
    findings = health.report(db, workspace_id)
    print("\n=== FINDINGS ===")
    if not findings:
        print("  none detected.")
        print("  This is not a claim that collection is complete: 'no gap detected'")
        print("  is what the coverage model can say, and it is weaker than 'no gap'.")
        return 0
    for finding in findings:
        print(f"  [{finding.kind}] source {finding.source_id}: {finding.detail}")
    return len(findings)


def main() -> int:
    raw = os.environ.get("COLLECTOR_WORKSPACE_ID")
    if not raw:
        print("COLLECTOR_WORKSPACE_ID is not set; refusing to guess which workspace to report on")
        return 2
    try:
        workspace_id = int(raw)
    except ValueError:
        print(f"COLLECTOR_WORKSPACE_ID is not a number: {raw!r}")
        return 2

    db = SessionLocal()
    try:
        # Every table below is under row-level security. Without this the
        # report comes back empty and reads as a healthy, quiet workspace
        # (see app/rls.py) — the exact failure this script exists to make
        # visible would be the failure of the script itself.
        scope_session_to_workspace(db, workspace_id)
        _accounts(db, workspace_id)
        _sources(db, workspace_id)
        _runs(db, workspace_id)
        found = _findings(db, workspace_id)
    except SQLAlchemyError as exc:
        print(f"could not read the database: {type(exc).__name__}: {exc}")
        return 2
    finally:
        db.close()

    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
