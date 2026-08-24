"""Every dashboard section must live inside a tab.

The page reached fourteen headings in one continuous scroll the same way
every long page does: twelve shipped phases, each appending its section to
the end of the template, each tested on its own and passing. Nobody looked
at the page as a whole afterwards, and the result put "ربط البوت" between
collection settings and account settings — which is exactly where a real
user went looking for it and did not find it.

Grouping is not the durable fix; grouping plus this file is. Without it the
next phase appends section fifteen after the last panel and the page
quietly starts coming apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "app/templates/dashboard.html"

EXPECTED_TABS = ["links", "collect", "bot", "security", "account"]


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_every_tab_has_a_matching_panel(markup: str) -> None:
    tabs = re.findall(r'role="tab"[^>]*id="tab-([a-z]+)"', markup)
    panels = re.findall(r'role="tabpanel"[^>]*id="panel-([a-z]+)"', markup)

    assert tabs == EXPECTED_TABS, f"tab order changed: {tabs}"
    assert sorted(panels) == sorted(EXPECTED_TABS), (
        f"a tab without its panel is a control that opens nothing: {panels}"
    )


def test_no_section_heading_sits_outside_a_panel(markup: str) -> None:
    """The check that actually catches an appended fifteenth section."""
    body = markup.split('role="tablist"', 1)[1]
    outside: list[str] = []
    depth = 0
    for chunk in re.split(r"(<section[^>]*>|</section>)", body):
        if chunk.startswith("<section"):
            depth += 1
        elif chunk == "</section>":
            depth -= 1
        elif depth == 0:
            outside.extend(re.findall(r"<h[23]>([^<]+)</h[23]>", chunk))

    assert outside == [], (
        f"these headings are outside every tab panel and will render below the "
        f"tabs as loose content: {outside}. Put each one inside the panel it "
        "belongs to, or add a tab for it."
    )


def test_links_is_the_tab_that_opens_first(markup: str) -> None:
    """Search and manual add are the daily use; the rest is occasional."""
    first_panel = re.search(r'role="tabpanel"[^>]*id="panel-([a-z]+)"([^>]*)>', markup)
    assert first_panel is not None
    assert first_panel.group(1) == "links"
    assert "hidden" not in first_panel.group(2), "the first tab must open unhidden"

    for key in EXPECTED_TABS[1:]:
        panel = re.search(rf'role="tabpanel"[^>]*id="panel-{key}"([^>]*)>', markup)
        assert panel is not None and "hidden" in panel.group(1), (
            f"panel-{key} must start hidden, or every section shows at once"
        )


def test_the_tablist_is_a_single_keyboard_stop(markup: str) -> None:
    """Roving tabindex: Tab reaches the tablist, arrows move inside it.

    Five focusable tabs would mean five presses of Tab before a keyboard
    user reaches the panel's own controls.
    """
    zero = re.findall(r'role="tab"[^>]*tabindex="0"', markup)
    assert len(zero) == 1, f"exactly one tab may be focusable, found {len(zero)}"


def test_the_tabs_carry_the_aria_wiring_a_screen_reader_needs(markup: str) -> None:
    for key in EXPECTED_TABS:
        tab = re.search(rf'<button role="tab" id="tab-{key}"[^>]*>', markup)
        assert tab is not None, f"tab-{key} missing"
        assert f'aria-controls="panel-{key}"' in tab.group(0)
        assert "aria-selected=" in tab.group(0)

        panel = re.search(rf'<section role="tabpanel" id="panel-{key}"[^>]*>', markup)
        assert panel is not None and f'aria-labelledby="tab-{key}"' in panel.group(0), (
            f"panel-{key} must name its tab, or it is an unlabelled region"
        )


def test_the_template_stays_free_of_inline_script(markup: str) -> None:
    """Phase 8d removed inline code so a strict CSP could be enforced."""
    assert "onclick=" not in markup and "onchange=" not in markup
    assert "<script>" not in markup, "an inline script would need a CSP exemption"
