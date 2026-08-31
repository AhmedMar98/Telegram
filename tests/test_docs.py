"""Guards for the documentation itself.

Documentation is the one part of a project with no compiler and no test
runner of its own, so it rots silently: a file moves and a doc keeps
pointing at where it used to be, and nobody finds out until a reader
follows the link.

These tests are deliberately mechanical. They cannot check whether a
sentence is *true* — that is what the "verify every claim against the
repository" rule (``DOCS_CONSOLIDATED.md`` §35) is for — but they can
check the claims a machine is able to check, which is most of the ones
that break on their own.

Historical note: this suite used to also compare several ``docs/*.md``
files against generator scripts (``scripts/env_report.py``,
``scripts/api_examples.py``) and cross-check a roadmap document against
an execution plan. All of ``docs/`` — 43 files plus the top-level
``README.md`` — was deliberately deleted and replaced by one file,
``DOCS_CONSOLIDATED.md`` (repository owner's decision, so that editing
documentation never means navigating between files). Those tests had
nothing left to compare and were removed rather than left to fail on a
directory that no longer exists. The generator scripts themselves are
untouched and still useful to run on demand — see
``DOCS_CONSOLIDATED.md`` §35 "أدوات مرجعية".
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# [text](target) — the inline markdown link form.
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# `path/to/file.py` in backticks: this project cites files that way far
# more often than it links them.
_BACKTICK_PATH = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|md|yml|yaml|html|js|css|txt|json))`")
# Inline code spans, stripped before link-scanning. Prose that *describes*
# markdown — `[label](url)` — is not a link to a file called "url", and a
# checker that cannot tell the difference gets switched off.
_CODE_SPAN = re.compile(r"`[^`]*`")


def _markdown_files() -> list[pathlib.Path]:
    return sorted(ROOT.glob("*.md"))


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
    missing = []
    for cited in set(_BACKTICK_PATH.findall(path.read_text(encoding="utf-8"))):
        # Absolute paths are HTTP routes (`/openapi.json`), not files.
        # Illustrative or third-party paths are never meant to be opened.
        if cited.startswith(("/", "http", "example", "path/")) or "/" not in cited:
            continue
        if not (ROOT / cited).exists():
            missing.append(cited)

    assert not missing, f"{path.name} cites paths that do not exist: {sorted(missing)}"


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
