"""Every workflow must say what the token is allowed to do.

A workflow with no ``permissions:`` block does not get *no* permissions —
it inherits whatever the repository default happens to be, which for an
older repository is read/write on almost everything. Ten of the eleven
workflows here were in that state: a scheduled job whose only real work
is a database write was running with a token that could push commits.

The exposure is not hypothetical. Every one of these jobs runs
third-party actions and installs dependencies from PyPI; a compromised
version of any of them inherits the token the job holds. Narrowing the
token is what decides whether that is an annoyance or a repository
takeover.

This file is generic on purpose: it walks the directory rather than
listing filenames, so a workflow added next year is covered the day it
lands instead of being forgotten.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github/workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))

# The one workflow that legitimately writes: it regenerates CHANGELOG.md
# from the merge history and commits the result. Listed by name so that a
# *second* workflow quietly granting itself write access is a test
# failure rather than a silent change.
ALLOWED_TO_WRITE = {"changelog.yml"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_there_are_workflows_to_check():
    """Guards the guard: a glob that silently matched nothing would make
    every parametrised test below vacuously pass."""
    assert len(WORKFLOWS) >= 11


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_declares_permissions(path: Path):
    workflow = _load(path)

    assert "permissions" in workflow, (
        f"{path.name} has no permissions block, so it runs with the repository default — "
        "which is far wider than any job here needs."
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_the_changelog_workflow_may_write(path: Path):
    permissions = _load(path)["permissions"]

    # An empty block means "no permissions at all", which is the strictest
    # possible answer and always acceptable.
    if permissions in ({}, None):
        return

    granted = {scope: level for scope, level in permissions.items() if level == "write"}
    if path.name in ALLOWED_TO_WRITE:
        assert granted == {"contents": "write"}, (
            f"{path.name} is allowed to write, but only to contents — it commits CHANGELOG.md."
        )
    else:
        assert not granted, (
            f"{path.name} grants write access to {sorted(granted)}. If it genuinely needs that, "
            "add it to ALLOWED_TO_WRITE with the reason; otherwise it is over-privileged."
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_grants_blanket_write(path: Path):
    """``permissions: write-all`` is the shorthand that undoes the point of
    this whole exercise, and it is a single word away from correct."""
    permissions = _load(path)["permissions"]

    assert permissions != "write-all", f"{path.name} grants write-all"


def test_a_workflow_that_never_checks_out_needs_nothing():
    """smoke.yml and report-run.yml only make network calls to the
    deployed application. Granting them even read access to the repository
    would be privilege they have no use for."""
    for name in ("smoke.yml", "report-run.yml"):
        workflow = _load(WORKFLOW_DIR / name)
        assert workflow["permissions"] in ({}, None), (
            f"{name} does not check out the repository, so it should hold no token permissions."
        )


def test_the_reusable_workflow_declares_its_own_permissions():
    """A called workflow with no block inherits the caller's, so this one
    would silently run at whatever level five different callers happened
    to hold. An empty block is a subset of any caller's, so declaring it
    can never break the call."""
    workflow = _load(WORKFLOW_DIR / "report-run.yml")

    assert "workflow_call" in workflow[True]
    assert workflow["permissions"] in ({}, None)
