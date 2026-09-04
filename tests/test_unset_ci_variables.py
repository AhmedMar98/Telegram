"""An unset CI variable is not an absent one.

``${{ vars.COLLECTOR_PACE_MIN_SECONDS }}`` for a repository variable
nobody has set does not drop out of the environment: GitHub expands it to
an empty string and exports it anyway. pydantic then reads an empty
string as a value that *was* supplied, ``float("")`` raises, and the
process dies while importing its own settings — before it has opened a
database connection or a Telegram session, which is why the failure looks
like nothing at all in the logs beyond a stack trace at import time.

That is what the scheduled collector did on every run for days: four
ValidationErrors from four settings whose whole purpose is to be
optional. The workflow's own comment said they "fall back to the defaults
in app/config.py when unset", and that sentence was true of the intent
and false of the behaviour.

These tests pin both halves of the contract. Blank means unset for the
tuning knobs, and blank stays fatal for the things where a working-looking
fallback would be worse than a crash — an empty ``DATABASE_URL`` is a
broken deployment, not a request for the local SQLite file.
"""

from __future__ import annotations

import re

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings


def _collector_workflow_env() -> dict[str, str]:
    """The env block the scheduled collector actually runs with."""
    with open(".github/workflows/collector.yml") as handle:
        workflow = yaml.safe_load(handle)
    for step in workflow["jobs"]["collect"]["steps"]:
        if step.get("name") == "Run collector":
            return step["env"]
    raise AssertionError("the collector workflow no longer has a 'Run collector' step")


def test_blank_collector_tuning_values_fall_back_to_their_defaults() -> None:
    """The exact production failure, as four empty strings."""
    settings = Settings(
        COLLECTOR_AUTO_DISCOVER="",
        COLLECTOR_PACE_MIN_SECONDS="",
        COLLECTOR_PACE_MAX_SECONDS="",
        COLLECTOR_PACE_BUDGET_SECONDS="",
    )

    assert settings.collector_auto_discover is True
    assert settings.collector_pace_min_seconds == 1.5
    assert settings.collector_pace_max_seconds == 4.0
    assert settings.collector_pace_budget_seconds == 240.0


def test_whitespace_is_blank_too() -> None:
    """A variable set to a space was not configured either."""
    assert Settings(COLLECTOR_PACE_MIN_SECONDS="   ").collector_pace_min_seconds == 1.5


def test_a_supplied_tuning_value_still_wins() -> None:
    """Falling back when blank must not mean ignoring what was set."""
    settings = Settings(COLLECTOR_PACE_MIN_SECONDS="2.5", COLLECTOR_AUTO_DISCOVER="false")

    assert settings.collector_pace_min_seconds == 2.5
    assert settings.collector_auto_discover is False


def test_an_unparsable_tuning_value_is_still_a_loud_failure() -> None:
    """Blank means unset. It does not mean 'accept anything'.

    A typo in a configured value is a mistake somebody made on purpose and
    should be reported, not rounded down to the default.
    """
    with pytest.raises(ValidationError):
        Settings(COLLECTOR_PACE_MIN_SECONDS="not-a-number")


def test_a_blank_credential_is_not_quietly_replaced_by_a_working_default() -> None:
    """The fallback is deliberately narrow.

    ``database_url`` carries a SQLite default for local development. If
    blank meant unset here, a deployment whose DATABASE_URL secret had been
    emptied would boot happily against an empty local file and report
    itself healthy while collecting into nothing — the precise failure this
    file exists to prevent, arrived at from the other direction.
    """
    assert Settings(DATABASE_URL="").database_url == ""


def test_every_variable_the_collector_workflow_passes_survives_being_blank() -> None:
    """Derived from the workflow, so a new knob cannot repeat the outage.

    Read from collector.yml rather than listed here on purpose: the next
    person to add a ``${{ vars.X }}`` line gets this test failing rather
    than a collector that dies at import on the hour.
    """
    blanks = {
        key: ""
        for key, value in _collector_workflow_env().items()
        if isinstance(value, str) and re.fullmatch(r"\$\{\{\s*vars\.\w+\s*\}\}", value.strip())
        if key.lower() in Settings.model_fields
    }

    assert blanks, "the collector workflow no longer passes any repository variables"
    Settings(**blanks)  # must not raise


def test_the_smoke_workflow_cannot_report_success_without_probing() -> None:
    """A monitor that measured nothing must not report health.

    The unconfigured branch used to ``exit 0``, so every smoke run in this
    repository's history was green while probing nothing at all. A passing
    check that asked no question is worse than no check: it is what lets a
    dead deployment sit behind a wall of green ticks.
    """
    with open(".github/workflows/smoke.yml") as handle:
        workflow = yaml.safe_load(handle)
    script = workflow["jobs"]["smoke"]["steps"][0]["run"]

    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if 'if [ -z "$BASE" ]' in line)
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "fi")
    guard = "\n".join(lines[start : end + 1])

    assert "APP_BASE_URL" in guard
    assert "exit 1" in guard
    assert "exit 0" not in guard


def test_the_smoke_workflow_checks_that_registration_is_still_closed() -> None:
    """Signup can reopen by an environment variable going missing.

    Nothing else in this workflow would notice: every page still returns
    its expected status while anyone on the internet can create an
    account. So the policy is probed, and the probe is pinned here
    against being quietly dropped.
    """
    with open(".github/workflows/smoke.yml") as handle:
        workflow = yaml.safe_load(handle)
    steps = workflow["jobs"]["smoke"]["steps"]

    named = [step for step in steps if step.get("name") == "Registration is invite-only"]
    assert named, "the smoke workflow no longer checks that registration is invite-only"

    script = named[0]["run"]
    assert "/auth/register" in script
    assert "403" in script, "the invite-only answer is not recognised"
    assert "422" in script, "the signup-is-open answer is not recognised"
    assert "exit 1" in script, "the workflow does not fail when signup is open"


def test_the_smoke_workflow_reports_the_revision_it_probed() -> None:
    """A green post-deploy check must say which revision went green.

    /readyz reads alembic_version on every call, and its body is the only
    view of the deployed revision from outside the host. Probing it and
    discarding the body leaves a smoke run unable to distinguish a new
    revision serving from the previous container still serving — so a
    migration could report success here without having been applied.
    """
    with open(".github/workflows/smoke.yml") as handle:
        workflow = yaml.safe_load(handle)
    steps = workflow["jobs"]["smoke"]["steps"]

    named = [step for step in steps if step.get("name") == "Report the deployed schema revision"]
    assert named, "the smoke workflow no longer reports the deployed schema revision"

    script = named[0]["run"]
    assert "/readyz" in script
    assert "schema_version" in script
    # Reported and checked for presence, not pinned to a value: a pinned
    # revision would fail every deploy that adds one.
    assert "exit 1" in script
