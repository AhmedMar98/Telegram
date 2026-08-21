"""The Content-Security-Policy, and the refactor that made it possible.

Idea 85. The screening in ``docs/17-phase8-screening.md`` recorded why
this could not ship as "add a header": the dashboard carried 49 inline
``on*=`` attributes, five ``<script>`` blocks and 29 ``style`` attributes,
and a nonce cannot apply to an ``on*=`` attribute at all. The header only
means anything once that inline code is gone, so most of what follows
guards the *absence* of inline code rather than the header itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import CONTENT_SECURITY_POLICY
from tests.conftest import register_workspace

TEMPLATES = sorted(Path("app/templates").glob("*.html"))
STATIC = sorted(Path("app/static").glob("*"))
PAGES = ("/login", "/register", "/dashboard")


# --- the header ------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_every_page_carries_the_policy(client: TestClient, path: str):
    register_workspace(client, email="csp@example.com", workspace_name="CSP")

    headers = client.get(path).headers

    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_api_responses_carry_it_too(client: TestClient):
    """Applied in middleware rather than per-route, so a route added later
    cannot be the one that forgets."""
    register_workspace(client, email="csp2@example.com", workspace_name="CSP2")

    assert "Content-Security-Policy" in client.get("/links").headers
    assert "Content-Security-Policy" in client.get("/healthz").headers


def test_the_policy_refuses_rather_than_merely_exists():
    """The point of this header is what it forbids. A policy carrying
    'unsafe-inline' on script-src permits precisely what XSS does, which
    would make it a header that reads as protection while providing none."""
    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY
    assert "style-src 'self'" in CONTENT_SECURITY_POLICY
    assert "object-src 'none'" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert "base-uri 'none'" in CONTENT_SECURITY_POLICY


def test_no_wildcard_source_slipped_in():
    """A single '*' or an http: scheme source would undo several directives
    at once, and reads almost identically at a glance."""
    assert " *" not in CONTENT_SECURITY_POLICY
    assert "http:" not in CONTENT_SECURITY_POLICY
    assert "https:" not in CONTENT_SECURITY_POLICY


# --- the absence of inline code, which is what makes the header real -------


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_carries_an_inline_event_handler(template: Path):
    """The regression this guards is subtle: an on*= attribute added later
    keeps working in development (nothing warns) and silently does nothing
    in production, because the CSP blocks it there."""
    found = re.findall(r'\son[a-z]+\s*=\s*"', template.read_text(encoding="utf-8"))

    assert found == [], f"{template.name} has inline handlers: {found}"


# The stylesheet is excluded because it cannot contain an inline style
# attribute by definition — and its own comment mentions the string it is
# replacing, which is what first tripped this check.
_MARKUP_SOURCES = TEMPLATES + [s for s in STATIC if s.suffix != ".css"]


@pytest.mark.parametrize("source", _MARKUP_SOURCES, ids=lambda p: p.name)
def test_nothing_emits_an_inline_style_attribute(source: Path):
    """Including the JavaScript, which builds markup as strings — a style
    attribute in one of those is inline CSS exactly like a hand-written
    one. Genuinely per-render values go through element.style instead,
    which the CSSOM allows and the CSP does not govern."""
    text = source.read_text(encoding="utf-8")

    assert 'style="' not in text, f"{source.name} emits an inline style attribute"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_carries_an_inline_script_or_style_block(template: Path):
    text = template.read_text(encoding="utf-8")

    scripts = [b for b in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S) if b.strip()]
    assert scripts == [], f"{template.name} has an inline <script> block"
    assert "<style>" not in text, f"{template.name} has an inline <style> block"


# --- the delegation that replaced the handlers -----------------------------


def _declared_actions() -> set[str]:
    """Every data-action named anywhere, templates and generated markup alike."""
    names: set[str] = set()
    for source in TEMPLATES + STATIC:
        names |= set(re.findall(r'data-action="([A-Za-z_$][\w$]*)"', source.read_text(encoding="utf-8")))
    return names


def _defined_functions() -> set[str]:
    names: set[str] = set()
    for source in STATIC:
        if source.suffix == ".js":
            text = source.read_text(encoding="utf-8")
            names |= set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text, re.M))
    return names


def test_every_declared_action_resolves_to_a_real_function():
    """Clicking 49 buttons by hand would not scale, and a typo in a
    data-action fails silently at runtime — the button simply does
    nothing. This catches the whole set at once."""
    declared = _declared_actions()
    defined = _defined_functions()

    assert len(declared) > 40, f"expected the full handler set, found {len(declared)}"
    missing = sorted(declared - defined)
    assert missing == [], f"data-action names with no function: {missing}"


def test_the_dispatcher_and_the_theme_script_are_served(client: TestClient):
    for asset in ("/static/theme.js", "/static/app.js", "/static/app.css", "/static/dashboard.js"):
        response = client.get(asset)
        assert response.status_code == 200, asset


def test_every_referenced_asset_exists(client: TestClient):
    """A 404 on a script under this CSP is not a cosmetic problem — the
    page simply stops working, with no inline fallback to carry it."""
    register_workspace(client, email="csp3@example.com", workspace_name="CSP3")

    for path in PAGES:
        body = client.get(path).text
        for asset in sorted(set(re.findall(r'(?:href|src)="(/static/[^"]+)"', body))):
            assert client.get(asset).status_code == 200, f"{path} references missing {asset}"
