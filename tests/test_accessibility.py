"""Accessibility and dashboard-health checks.

Two kinds of assertion live here. The contrast test is a real measurement
against the WCAG formula, not a look-at-it judgement — it is what found
that the form borders scored 1.54 where 3.0 is required. The markup tests
pin the things that silently regress: an icon button that loses its name,
a control that loses its label.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import register_workspace

# --- WCAG 2.1 contrast, computed rather than eyeballed --------------------

AA_NORMAL_TEXT = 4.5
AA_NON_TEXT = 3.0  # 1.4.11: UI component boundaries, e.g. an input border


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(foreground: str, background: str) -> float:
    a, b = _luminance(foreground), _luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def _palette(theme: str) -> dict[str, str]:
    """Read the live tokens out of the stylesheet rather than duplicating them.

    A copy of the palette in the test would keep passing after someone
    changed the stylesheet, which is exactly the regression worth catching.

    The stylesheet moved from a <style> block in base.html to
    app/static/app.css when the CSP dropped 'unsafe-inline' from
    style-src; the tokens and this test's purpose are unchanged.
    """
    css = (__import__("pathlib").Path(__file__).resolve().parent.parent / "app/static/app.css").read_text(
        encoding="utf-8"
    )

    if theme == "light":
        block = css.split(":root {", 1)[1].split("}", 1)[0]
    else:
        block = css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]

    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", block))


def test_the_contrast_formula_agrees_with_known_values():
    """Guards the measurement itself: black on white is exactly 21:1."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    ("foreground", "background", "minimum", "label"),
    [
        ("fg", "bg", AA_NORMAL_TEXT, "body text on the page"),
        ("fg", "card-bg", AA_NORMAL_TEXT, "body text on a card"),
        ("muted", "bg", AA_NORMAL_TEXT, "muted text on the page"),
        ("muted", "card-bg", AA_NORMAL_TEXT, "muted text on a card"),
        ("accent", "bg", AA_NORMAL_TEXT, "links on the page"),
        ("accent", "card-bg", AA_NORMAL_TEXT, "links on a card"),
        ("danger", "bg", AA_NORMAL_TEXT, "error text"),
        ("fg", "tag-bg", AA_NORMAL_TEXT, "highlighted search term"),
        # The one that was failing: 1.54 in light, 1.56 in dark.
        ("border", "bg", AA_NON_TEXT, "input border on the page"),
        ("border", "card-bg", AA_NON_TEXT, "input border on a card"),
    ],
)
def test_palette_meets_wcag_aa(theme: str, foreground: str, background: str, minimum: float, label: str):
    palette = _palette(theme)
    ratio = contrast(palette[foreground], palette[background])
    assert ratio >= minimum, f"{theme}: {label} scores {ratio:.2f}, needs {minimum}"


def test_both_themes_define_every_token_the_tests_check():
    """A token defined only under a media query would make the test above
    silently skip the case it is meant to cover."""
    expected = {"bg", "fg", "muted", "border", "card-bg", "tag-bg", "accent", "danger"}
    assert expected <= set(_palette("light"))
    assert expected <= set(_palette("dark"))


# --- markup that must not regress -----------------------------------------


@pytest.fixture
def dashboard(client: TestClient) -> str:
    """The dashboard as a browser actually receives it: markup plus every
    stylesheet and script it links.

    The page used to carry its CSS and JS inline, so the HTML alone was
    the whole story. Since the CSP forced both out into /static, asserting
    on the markup by itself would quietly stop covering the styles and
    behaviour these tests exist to check — so the fixture follows the
    references the page declares.
    """
    register_workspace(client, email="a11y@example.com", workspace_name="A11y")
    response = client.get("/dashboard")
    assert response.status_code == 200

    bundle = [response.text]
    for path in sorted(set(re.findall(r'(?:href|src)="(/static/[^"]+)"', response.text))):
        asset = client.get(path)
        assert asset.status_code == 200, f"{path} is referenced but not served"
        bundle.append(asset.text)
    return "\n".join(bundle)


def test_the_document_declares_language_and_direction(dashboard: str):
    assert 'lang="ar"' in dashboard
    assert 'dir="rtl"' in dashboard


def test_every_filter_control_has_a_label(dashboard: str):
    for control_id in ("q", "category", "sort", "aliveFilter", "channelFilter", "languageFilter"):
        assert f'for="{control_id}"' in dashboard, f"#{control_id} has no <label>"


def test_status_messages_have_a_live_region(dashboard: str):
    assert 'aria-live="polite"' in dashboard
    assert 'id="srStatus"' in dashboard


def test_the_undo_offer_uses_alert_not_polite(dashboard: str):
    """An undo window expires in five seconds; waiting for a pause in
    speech would make the announcement arrive after the chance is gone."""
    undo_markup = dashboard.split('id="undoBar"', 1)[1].split(">", 1)[0]
    assert 'role="alert"' in undo_markup


def test_hidden_panels_carry_no_inline_display(dashboard: str):
    """An inline ``display`` outranks the user agent's
    ``[hidden] { display: none }``, so the panel stays in the layout *and*
    in the tab order while invisible. That is how the undo bar ended up
    focusable-but-unseeable for keyboard users.
    """
    for panel_id in ("undoBar", "quickStart", "collectorWarning"):
        markup = dashboard.split(f'id="{panel_id}"', 1)[1].split(">", 1)[0]
        assert "hidden" in markup, f"#{panel_id} is not hidden by default"
        assert "display:" not in markup.replace(" ", ""), (
            f"#{panel_id} sets display inline, which defeats [hidden]"
        )


def test_hidden_is_enforced_in_the_stylesheet(dashboard: str):
    assert "[hidden] { display: none !important; }" in dashboard


def test_the_vitality_bar_is_not_an_unlabelled_decoration(dashboard: str):
    bar = dashboard.split('id="vitalityBar"', 1)[1].split(">", 1)[0]
    assert "aria-label" in bar


def test_icon_only_buttons_are_named(dashboard: str):
    """The star, copy and archive buttons carry a symbol and nothing else;
    without aria-label a screen reader announces "button" or the emoji.

    The favourite and archive labels are interpolated at render time
    because their wording depends on the current state, so the assertion
    is on the attribute being present with a non-empty value, plus on the
    strings that produce it existing in the page.
    """
    assert 'aria-label="انسخ الرابط"' in dashboard
    assert 'aria-label="احذف الرابط"' in dashboard
    assert 'aria-label="${favLabel}"' in dashboard
    assert 'aria-label="${archLabel}"' in dashboard
    # Both branches of each state-dependent label must exist, or one state
    # renders an empty name.
    for wording in ("أزل من المفضّلة", "أضف إلى المفضّلة", "أعد من الأرشيف", "أرشف"):
        assert wording in dashboard, f"no wording for {wording}"


def test_reduced_motion_is_honoured(dashboard: str):
    assert "prefers-reduced-motion" in dashboard


def test_focus_is_visible(dashboard: str):
    """Some resets remove the default outline; without a replacement,
    keyboard navigation becomes invisible."""
    assert ":focus-visible" in dashboard


def test_urls_are_bidi_isolated(dashboard: str):
    """A Latin URL inside an RTL document reorders without an explicit
    direction, so a correctly stored link renders scrambled."""
    assert "unicode-bidi: isolate" in dashboard


def test_touch_targets_have_a_minimum_size(dashboard: str):
    assert "min-height: 2.25rem" in dashboard


# --- collector health -----------------------------------------------------


def test_a_workspace_that_never_collected_is_not_reported_as_stalled(client: TestClient):
    """Manual-only use is a legitimate way to use this; warning about a
    collector that was never set up would be crying wolf."""
    register_workspace(client, email="never@example.com", workspace_name="Never")

    collection = client.get("/links/stats").json()["collection"]

    assert collection["last_run_at"] is None
    assert collection["hours_since_last_run"] is None
    assert collection["looks_stalled"] is False


def test_a_recent_collector_run_is_healthy(client: TestClient):
    from datetime import timedelta

    from app.database import SessionLocal
    from app.models import AuditLog, User
    from app.timeutil import utcnow

    register_workspace(client, email="fresh@example.com", workspace_name="Fresh")
    db = SessionLocal()
    try:
        workspace_id = db.query(User).filter(User.email == "fresh@example.com").one().workspace_id
        db.add(
            AuditLog(
                workspace_id=workspace_id,
                user_id=None,
                action="collector.run",
                created_at=utcnow() - timedelta(hours=2),
            )
        )
        db.commit()
    finally:
        db.close()

    collection = client.get("/links/stats").json()["collection"]

    assert collection["looks_stalled"] is False
    assert 1.5 < collection["hours_since_last_run"] < 2.5


def test_a_long_silent_collector_is_reported_as_stalled(client: TestClient):
    """The failure mode with no symptom: everything keeps working, the
    collection just stops growing."""
    from datetime import timedelta

    from app.database import SessionLocal
    from app.models import AuditLog, User
    from app.timeutil import utcnow

    register_workspace(client, email="stalled@example.com", workspace_name="Stalled")
    db = SessionLocal()
    try:
        workspace_id = db.query(User).filter(User.email == "stalled@example.com").one().workspace_id
        db.add(
            AuditLog(
                workspace_id=workspace_id,
                user_id=None,
                action="collector.run",
                created_at=utcnow() - timedelta(days=3),
            )
        )
        db.commit()
    finally:
        db.close()

    collection = client.get("/links/stats").json()["collection"]

    assert collection["looks_stalled"] is True
    assert collection["hours_since_last_run"] > 24


def test_collection_health_does_not_leak_across_workspaces(client: TestClient):
    from datetime import timedelta

    from app.database import SessionLocal
    from app.models import AuditLog, User
    from app.timeutil import utcnow

    register_workspace(client, email="ch1@example.com", workspace_name="CH1")
    db = SessionLocal()
    try:
        workspace_id = db.query(User).filter(User.email == "ch1@example.com").one().workspace_id
        db.add(
            AuditLog(
                workspace_id=workspace_id,
                user_id=None,
                action="collector.run",
                created_at=utcnow() - timedelta(hours=1),
            )
        )
        db.commit()
    finally:
        db.close()
    client.post("/auth/logout")

    register_workspace(client, email="ch2@example.com", workspace_name="CH2")
    assert client.get("/links/stats").json()["collection"]["last_run_at"] is None


def test_other_audit_actions_do_not_count_as_a_collector_run(client: TestClient):
    """Deleting a link writes an audit row too; reading any row as
    "collection happened" would hide a genuinely stopped collector."""
    register_workspace(client, email="otheraudit@example.com", workspace_name="OtherAudit")
    client.post("/links", json={"text": "https://example.com/a.pdf"})
    link_id = client.get("/links").json()["items"][0]["id"]
    client.delete(f"/links/{link_id}")

    assert client.get("/links/stats").json()["collection"]["last_run_at"] is None
