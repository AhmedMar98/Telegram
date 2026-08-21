"""Guards for the documentation itself.

Phase 12. Documentation is the one part of a project with no compiler and
no test runner of its own, so it rots silently: a file moves and three
docs keep pointing at where it used to be, and nobody finds out until a
reader follows the link.

These tests are deliberately mechanical. They cannot check whether a
sentence is *true* — that is what the "verify every claim against the
repository" rule in ``docs/03-engineering-prompt.md`` is for — but they
can check the claims a machine is able to check, which is most of the
ones that break on their own.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# [text](target) — the inline markdown link form.
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# `path/to/file.py` in backticks: this project cites files that way far
# more often than it links them.
_BACKTICK_PATH = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|md|yml|yaml|html|js|css|txt|json))`")
# Inline code spans, stripped before link-scanning. Prose that *describes*
# markdown — `[label](url)` in docs/10 — is not a link to a file called
# "url", and a checker that cannot tell the difference gets switched off.
_CODE_SPAN = re.compile(r"`[^`]*`")

# The as-is analysis describes the repository as it stood *before* this
# rewrite. Its paths are supposed to be gone: that document is a record of
# what was replaced, and "fixing" its citations would destroy the only
# account of the starting point.
HISTORICAL = {"00-as-is.md"}


def _markdown_files() -> list[pathlib.Path]:
    return sorted([*DOCS.glob("*.md"), ROOT / "README.md", *ROOT.glob("*.md")])


def _relative_targets(text: str) -> set[str]:
    out = set()
    for target in _MD_LINK.findall(_CODE_SPAN.sub("", text)):
        target = target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        out.add(target)
    return out


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_every_relative_link_points_at_something_that_exists(path: pathlib.Path):
    """A link to a file that moved is worse than no link: it says the
    answer exists and sends the reader to nothing."""
    broken = []
    for target in _relative_targets(path.read_text(encoding="utf-8")):
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            broken.append(target)

    assert not broken, f"{path.name} links to files that do not exist: {sorted(broken)}"


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_every_cited_repository_path_exists(path: pathlib.Path):
    """This project cites source files in backticks constantly. A citation
    to a renamed module is a factual error in the prose, not a broken
    link, and nothing else in the suite would notice it."""
    if path.name in HISTORICAL:
        pytest.skip(f"{path.name} documents the repository as it was before the rewrite")

    missing = []
    for cited in set(_BACKTICK_PATH.findall(path.read_text(encoding="utf-8"))):
        # Absolute paths are HTTP routes (`/openapi.json`), not files.
        # Illustrative or third-party paths are never meant to be opened.
        if cited.startswith(("/", "http", "example", "path/")) or "/" not in cited:
            continue
        if not (ROOT / cited).exists():
            missing.append(cited)

    assert not missing, f"{path.name} cites paths that do not exist: {sorted(missing)}"


def test_the_environment_variable_table_is_current():
    """Idea 234 sits on the *continuous* track in docs/06 §4 — "updated the
    moment any new secret appears". It was never created, so the thing
    meant to track every new secret tracked none of them. Generating it and
    failing here when it drifts is what makes that promise mechanical
    rather than remembered."""
    doc = DOCS / "29-env-vars.md"
    before = doc.read_text(encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/env_report.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": "sqlite:///./local.db"},
    )
    assert result.returncode == 0, result.stderr

    after = doc.read_text(encoding="utf-8")
    if after != before:
        doc.write_text(before, encoding="utf-8")
    assert after == before, "docs/29-env-vars.md is stale — run: python scripts/env_report.py"


def test_every_workflow_secret_reaches_the_env_template():
    """The gap this found on its first run.

    APP_API_KEY and APP_BASE_URL are what phase 10's scheduled runs use to
    report their outcome, and neither appeared in .env.example. A fresh
    deployment would have had a permanently empty status board — which
    reads exactly like "every workflow stopped", the one ambiguity that
    phase deliberately documented and then reintroduced by omission.
    """
    from scripts.env_report import CI_ONLY, _env_example_keys, _workflow_refs

    missing = sorted(set(_workflow_refs()) - CI_ONLY - _env_example_keys())

    assert not missing, f".env.example never mentions: {missing}"


def test_the_curl_examples_are_current():
    doc = DOCS / "28-api-examples.md"
    before = doc.read_text(encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/api_examples.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": "sqlite:///:memory:"},
    )
    assert result.returncode == 0, result.stderr

    after = doc.read_text(encoding="utf-8")
    if after != before:
        doc.write_text(before, encoding="utf-8")
    assert after == before, "docs/28-api-examples.md is stale — run: python scripts/api_examples.py"


def test_the_curl_doc_agrees_with_the_authentication_boundary():
    """Two independent records of the same fact, forced to match.

    The generator classifies each endpoint from the live dependency tree;
    tests/test_auth_boundary.py declares the same set by hand. They can
    only disagree if one of them is wrong, and the failure this catches is
    the dangerous direction: the first version of the generator reported
    DELETE /auth/api-keys/{key_id} as needing **no authentication**,
    because FastAPI keeps the ``{key_id:int}`` converter that OpenAPI
    strips, so the route lookup missed and fell through to "public".
    """
    from scripts.api_examples import SESSION_ONLY, _auth_of, _openapi_path, _routes
    from tests.test_auth_boundary import SESSION_ONLY as DECLARED

    generated = {
        (method, route.path) for route in _routes() for method in route.methods if _auth_of(route) == SESSION_ONLY
    }
    # The declared set keeps the converters; the generator's lookup key
    # strips them. Compare on one spelling.
    normalise = {(method, _openapi_path(path)) for method, path in DECLARED}
    generated = {(method, _openapi_path(path)) for method, path in generated}

    assert generated == normalise, f"difference: {sorted(generated ^ normalise)}"


def test_every_endpoint_appears_in_the_curl_doc():
    """A route added later with no example is a hole in "every endpoint"."""
    import os as _os

    _os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from app.main import app

    doc = (DOCS / "28-api-examples.md").read_text(encoding="utf-8")
    missing = [
        f"{method.upper()} {path}"
        for path, methods in app.openapi()["paths"].items()
        for method in methods
        if f"### `{method.upper()} {path}`" not in doc
    ]

    assert not missing, f"no curl example for: {sorted(missing)}"


# Arabic-Indic digits, so a phase number can be compared across documents.
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# The trailing marker is NOT optional here, and that is the whole point.
# Written as `(...)?` the lazy `[^\n]*?` before it matches nothing and the
# group comes back empty for every heading, so plan_status is empty, so the
# comparison below compares nothing and passes always. That is exactly what
# it did on its first run: deliberately breaking the roadmap did not fail
# the test. A guard that silently checks nothing is the failure mode this
# file exists to prevent, so the parse is asserted non-empty below too.
_PLAN_HEADING = re.compile(r"^### ~*المرحلة ([٠-٩]+[أب]?)[^\n]*?(✅|◐|⛔)", re.MULTILINE)
_ROADMAP_ROW = re.compile(r"^\| ([٠-٩]+[أب]?) — [^|]*\| (✅|◐|⛔)", re.MULTILINE)


def _phase_key(raw: str) -> str:
    return raw.translate(_AR_DIGITS)


def test_the_roadmap_agrees_with_the_execution_plan():
    """Two documents stating the same fact, forced to agree.

    docs/27-roadmap.md is the reader-facing summary of
    docs/06-execution-plan.md. Nothing links them, so the roadmap keeps
    saying whatever it said when it was written — and it did: it still
    called phase 12 "in progress" after phase 12 had finished, in the same
    commit that finished it.

    A roadmap that misstates status is worse than no roadmap, because it is
    the document someone reads *instead of* the plan.
    """
    plan = (DOCS / "06-execution-plan.md").read_text(encoding="utf-8")
    roadmap = (DOCS / "27-roadmap.md").read_text(encoding="utf-8")

    plan_status = {_phase_key(num): mark for num, mark in _PLAN_HEADING.findall(plan) if mark}
    roadmap_status = {_phase_key(num): mark for num, mark in _ROADMAP_ROW.findall(roadmap)}

    # Guards the guard, in both directions: either parse coming back empty
    # would make the comparison vacuous.
    assert len(roadmap_status) >= 10, f"roadmap table shape changed — parsed {len(roadmap_status)} rows"
    assert len(plan_status) >= 10, f"plan heading shape changed — parsed {len(plan_status)} phases"

    disagreements = {
        phase: (plan_status[phase], roadmap_status[phase])
        for phase in roadmap_status
        if phase in plan_status and plan_status[phase] != roadmap_status[phase]
    }

    assert not disagreements, f"plan vs roadmap (plan, roadmap): {disagreements}"


def test_every_completed_phase_appears_in_the_roadmap():
    """A phase that finished and never reached the reader-facing page is a
    phase whose completion nobody outside the plan can see."""
    plan = (DOCS / "06-execution-plan.md").read_text(encoding="utf-8")
    roadmap = (DOCS / "27-roadmap.md").read_text(encoding="utf-8")

    # 6b and 11 are deliberately outside the status table — each has its own
    # section — so they are checked as *mentioned*, not as table rows.
    listed = {_phase_key(num) for num, _ in _ROADMAP_ROW.findall(roadmap)}
    mentioned = {_phase_key(num) for num in re.findall(r"([٠-٩]+[أب]?) —", roadmap)}

    finished = {_phase_key(num) for num, mark in _PLAN_HEADING.findall(plan) if mark in ("✅", "◐")}

    missing = sorted(finished - listed - mentioned)
    assert not missing, f"phases absent from docs/27-roadmap.md entirely: {missing}"
