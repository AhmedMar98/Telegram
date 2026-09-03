"""The one view that answers "is collection working", tested for honesty.

The script is a report, so what matters is not that it runs but that it
cannot say something untrue. Three properties, and each of them is a claim
this repository has been burned by before:

- an empty workspace must not read as a working one;
- "assigned but never attempted" must show up as a finding, because that
  is what a runtime that is not running actually looks like;
- the exit code must distinguish "nothing wrong" from "findings", so a
  cron or a person can act on it without parsing prose.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app import assignments
from app.models import Channel, TelegramAccount, Workspace

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_collection.py"


def _run(workspace_id: int | str | None, database_url: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": database_url,
        "SECRET_KEY": "test-secret-key",
        "ENVIRONMENT": "test",
    }
    if workspace_id is not None:
        env["COLLECTOR_WORKSPACE_ID"] = str(workspace_id)
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO, env=env, capture_output=True, text=True)


@pytest.fixture
def file_db(tmp_path):
    """A real file-backed database the subprocess can also open.

    The suite's in-memory SQLite lives inside this process and a
    subprocess would find an empty one, so the script would be tested
    against nothing and pass.
    """
    from sqlalchemy import create_engine

    from app.database import Base

    path = tmp_path / "check.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    yield url, engine
    engine.dispose()


def _seed(engine, *, assign: bool) -> int:
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    with Session() as db:
        workspace = Workspace(name="deploy")
        db.add(workspace)
        db.flush()
        account = TelegramAccount(workspace_id=workspace.id, label="primary", session_string="x")
        db.add(account)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="-1001", username="demo")
        db.add(channel)
        db.flush()
        if assign:
            assignments.assign(db, channel, account.id, reason="fixture")
        db.commit()
        return workspace.id


def test_it_refuses_to_guess_which_workspace_to_report_on(file_db):
    url, _ = file_db
    result = _run(None, url)
    assert result.returncode == 2
    assert "COLLECTOR_WORKSPACE_ID" in result.stdout


def test_an_unassigned_source_is_reported_as_belonging_to_nobody(file_db):
    url, engine = file_db
    workspace_id = _seed(engine, assign=False)
    result = _run(workspace_id, url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "assigned to=NOBODY" in result.stdout
    assert "never attempted by the runtime" in result.stdout


def test_assigned_and_never_attempted_is_a_finding_with_a_non_zero_exit(file_db):
    """What a runtime that was never started actually looks like.

    Sabotage: drop the ``stalled`` call from ``health.report`` and this
    exits 0 over a workspace where nothing has ever collected.
    """
    url, engine = file_db
    workspace_id = _seed(engine, assign=True)
    result = _run(workspace_id, url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[STALLED]" in result.stdout


def test_it_never_claims_collection_is_complete(file_db):
    """ "No findings" is not "no gaps", and the report has to say so."""
    url, engine = file_db
    workspace_id = _seed(engine, assign=False)
    result = _run(workspace_id, url)
    assert "weaker than 'no gap'" in result.stdout
    for forbidden in ("healthy", "all good", "success rate"):
        assert forbidden not in result.stdout.lower()


def test_an_empty_run_table_explains_itself_rather_than_implying_failure(file_db):
    """collection_runs is empty in the currently deployed configuration.

    Only app/runtime/worker.py writes it, and that process is not deployed
    — so an empty table is expected, and a report that let it read as
    "nothing ever collected" would send an operator hunting a fault that
    is not there.
    """
    url, engine = file_db
    workspace_id = _seed(engine, assign=False)
    result = _run(workspace_id, url)
    assert "none recorded" in result.stdout
    assert "is not evidence that nothing collected" in result.stdout
