"""The check that makes a database cutover safe to commit to.

``pg_restore`` exits 0 on a run that skipped rows — it prints warnings and
carries on. If nobody read the console the migration looks finished, the
app starts, the dashboard loads, and part of the archive is simply gone.
Nobody notices until they search for something old and it is not there.

So the comparison logic is worth testing directly, because the moment it
reports a false "verified" is the moment the source database gets retired
and the missing rows become unrecoverable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.database import Base
from app.models import Workspace
from scripts.verify_migration import compare

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts/verify_migration.py"


# --- the comparison itself --------------------------------------------------


def test_identical_counts_report_no_problems():
    counts = {"links": 1200, "channels": 8, "users": 1}

    assert compare(counts, dict(counts)) == []


def test_a_short_target_is_reported_with_the_shortfall():
    problems = compare({"links": 1200}, {"links": 1198})

    assert len(problems) == 1
    assert "links" in problems[0]
    assert "-2" in problems[0]


def test_a_missing_target_table_is_not_treated_as_empty():
    """The confusion this script exists to prevent: "the table was never
    created" must never read as "the table was empty on both sides"."""
    problems = compare({"links": 1200}, {"links": -1})

    assert len(problems) == 1
    assert "missing on the target" in problems[0]


def test_a_table_absent_from_both_sides_is_not_a_migration_problem():
    """A model with no migration yet is a different bug; not this one."""
    assert compare({"future_table": -1}, {"future_table": -1}) == []


def test_an_unexpected_extra_table_on_the_target_is_reported():
    problems = compare({"links": -1}, {"links": 5})

    assert len(problems) == 1
    assert "missing on the source" in problems[0]


def test_every_mismatched_table_is_reported_not_just_the_first():
    """An operator who fixes only the table they were told about, then
    re-runs and passes, would still have lost the others."""
    problems = compare({"links": 10, "channels": 5}, {"links": 9, "channels": 4})

    assert len(problems) == 2


# --- end to end, against two real databases ---------------------------------


@pytest.fixture
def two_databases(tmp_path):
    """A source and a target, each a real database with the real schema."""

    def build(name: str, workspaces: int) -> str:
        url = f"sqlite:///{tmp_path / name}"
        engine = create_engine(url)
        Base.metadata.create_all(bind=engine)
        with Session(engine) as session:
            for index in range(workspaces):
                session.add(Workspace(name=f"ws{index}"))
            session.commit()
        engine.dispose()
        return url

    return build


def _run(source: str, target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SOURCE_DATABASE_URL": source,
            "TARGET_DATABASE_URL": target,
        },
        cwd=REPO_ROOT,
    )


def test_a_real_shortfall_exits_non_zero(two_databases):
    source = two_databases("src.db", 3)
    target = two_databases("tgt.db", 2)

    result = _run(source, target)

    assert result.returncode == 1
    assert "NOT verified" in result.stderr
    assert "Do not retire the source database" in result.stderr


def test_two_matching_databases_exit_zero(two_databases):
    source = two_databases("src.db", 3)
    target = two_databases("tgt.db", 3)

    result = _run(source, target)

    assert result.returncode == 0
    assert "Verified" in result.stdout


def test_pointing_both_urls_at_one_database_is_refused(two_databases):
    """Otherwise the easiest possible mistake — pasting the same URL twice
    — produces a confident, meaningless "verified"."""
    source = two_databases("src.db", 3)

    result = _run(source, source)

    assert result.returncode == 1
    assert "same database" in result.stderr


def test_missing_environment_says_why_urls_are_not_arguments():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=REPO_ROOT,
    )

    assert result.returncode == 1
    assert "process list" in result.stderr
