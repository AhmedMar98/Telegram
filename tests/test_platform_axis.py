"""The platform axis: which service a link points at.

Separate from ``category`` on purpose, and these tests exist to keep it
separate. The failure this guards against is not "the classifier is
wrong" — it is somebody deciding the two columns are redundant and
collapsing them, at which point one of the two questions stops being
answerable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.classifier.platform import DEFAULT_PLATFORM, PLATFORMS, link_platform
from tests.conftest import register_workspace


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/+AbcDef123", "telegram"),
        ("https://t.me/joinchat/AAAA", "telegram"),
        ("https://telegram.me/channel", "telegram"),
        ("https://wa.me/966500000000", "whatsapp"),
        ("https://chat.whatsapp.com/XyZ", "whatsapp"),
        ("https://youtu.be/dQw4", "youtube"),
        ("https://www.youtube.com/watch?v=1", "youtube"),
        ("https://x.com/user/status/1", "twitter"),
        ("https://vm.tiktok.com/abc", "tiktok"),
        ("https://drive.google.com/file/d/1", "drive"),
        ("https://example.com/paper.pdf", "web"),
    ],
)
def test_the_host_decides_the_platform(url: str, expected: str):
    assert link_platform(url) == expected


def test_a_query_string_mentioning_a_service_is_not_that_service():
    """The tempting implementation searches the whole URL for "whatsapp".

    This is what it gets wrong: a referral parameter is not a platform,
    and treating it as one would file an ordinary web link under WhatsApp
    forever.
    """
    assert link_platform("https://example.com/page?ref=whatsapp&via=telegram") == "web"


def test_a_host_that_merely_ends_in_a_known_name_is_not_a_match():
    """Suffix matching happens at label boundaries or it is a substring
    search wearing a costume — "nottiktok.com" is not TikTok."""
    assert link_platform("https://nottiktok.com/x") == "web"
    assert link_platform("https://faket.me/x") == "web"


def test_credentials_and_ports_do_not_hide_the_host():
    assert link_platform("https://user:pw@t.me:443/channel") == "telegram"


def test_a_subdomain_falls_through_to_its_parent():
    assert link_platform("https://beta.tiktok.com/x") == "tiktok"


@pytest.mark.parametrize("junk", ["", "not a url", "://", "mailto:a@b.c"])
def test_unparseable_input_never_raises(junk: str):
    """This runs on every collected link. A classifier that can raise is a
    collector that can die on one malformed URL."""
    assert link_platform(junk) == DEFAULT_PLATFORM


def test_every_returned_platform_is_declared():
    """Guards the pair: a host table entry naming a platform missing from
    PLATFORMS would filter and display as an unknown value."""
    from app.classifier.platform import _HOSTS

    undeclared = sorted(set(_HOSTS.values()) - set(PLATFORMS))
    assert undeclared == [], f"host table names platforms not in PLATFORMS: {undeclared}"


# --- the axis end to end ---------------------------------------------------


def _add(client: TestClient, url: str) -> None:
    assert client.post("/links", json={"text": url}).status_code in (200, 201)


def test_a_stored_link_carries_its_platform(client: TestClient):
    register_workspace(client, email="plat@example.com", workspace_name="Plat")
    _add(client, "https://t.me/somechannel/12")

    items = client.get("/links").json()["items"]

    assert items[0]["platform"] == "telegram"


def test_platform_and_category_are_independent_filters(client: TestClient):
    """The reason there are two columns, stated as a test.

    Two Telegram links in different categories and two course links on
    different platforms — so neither filter can stand in for the other,
    and a future "these are redundant, merge them" cannot pass.
    """
    register_workspace(client, email="axes@example.com", workspace_name="Axes")
    _add(client, "https://t.me/films/1/movie.mkv")
    _add(client, "https://t.me/books/2/course.pdf")
    _add(client, "https://example.com/other-course.pdf")

    telegram = client.get("/links", params={"platform": "telegram"}).json()
    assert telegram["total"] == 2, "platform must not be narrowed by category"

    both = client.get("/links", params={"platform": "telegram", "category": "books_courses"}).json()
    assert both["total"] == 1, "the two filters must intersect, not replace each other"
    assert "course.pdf" in both["items"][0]["url"]


def test_several_platforms_can_be_asked_for_at_once(client: TestClient):
    register_workspace(client, email="multi@example.com", workspace_name="Multi")
    _add(client, "https://t.me/a/1")
    _add(client, "https://wa.me/966500000000")
    _add(client, "https://example.com/x.pdf")

    both = client.get("/links", params={"platform": "telegram,whatsapp"}).json()

    assert both["total"] == 2


def test_the_stats_panel_reports_the_whole_platform_split(client: TestClient):
    """Not a top N. There are eleven platforms by construction, and a
    truncated answer to "where do my links live" is not an answer."""
    register_workspace(client, email="split@example.com", workspace_name="Split")
    _add(client, "https://t.me/a/1")
    _add(client, "https://t.me/b/2")
    _add(client, "https://example.com/x.pdf")

    platforms = dict(map(tuple, client.get("/links/stats").json()["platforms"]))

    assert platforms["telegram"] == 2
    assert platforms["web"] == 1
