"""Channel-scoped search, workspace rename, and the theme toggle.

The two API features get the same isolation treatment as everything else
that touches tenant data; the theme toggle is client-side only, so what is
tested there is that the page actually ships the three-state machinery
rather than a two-state one that can never return to following the OS.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.models import Link
from tests.conftest import register_workspace


def _seed_link(workspace_id: int, channel_id: int, message_id: int, url: str) -> None:
    db = SessionLocal()
    try:
        db.add(
            Link(
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_id=message_id,
                url=url,
                url_hash=hash_url(url),
                domain="example.com",
                category="other",
                confidence=0.5,
                classified_by="rules",
            )
        )
        db.commit()
    finally:
        db.close()


# --- channel-scoped search -------------------------------------------------


def test_search_can_be_scoped_to_one_channel(client: TestClient):
    register_workspace(client, email="chan@example.com", workspace_name="Chan Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    first = client.post("/channels", json={"tg_channel_id": "1", "username": "one"}).json()
    second = client.post("/channels", json={"tg_channel_id": "2", "username": "two"}).json()

    _seed_link(workspace_id, first["id"], 1, "https://example.com/from-one.pdf")
    _seed_link(workspace_id, second["id"], 2, "https://example.com/from-two.pdf")

    scoped = client.get("/links", params={"channel_id": first["id"]}).json()

    assert scoped["total"] == 1
    assert scoped["items"][0]["url"] == "https://example.com/from-one.pdf"
    assert client.get("/links").json()["total"] == 2


def test_channel_filter_combines_with_a_search_term(client: TestClient):
    register_workspace(client, email="chan2@example.com", workspace_name="Chan2")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    first = client.post("/channels", json={"tg_channel_id": "1", "username": "one"}).json()
    second = client.post("/channels", json={"tg_channel_id": "2", "username": "two"}).json()

    _seed_link(workspace_id, first["id"], 1, "https://example.com/report.pdf")
    _seed_link(workspace_id, second["id"], 2, "https://example.com/report-two.pdf")

    both = client.get("/links", params={"q": "report"}).json()
    scoped = client.get("/links", params={"q": "report", "channel_id": second["id"]}).json()

    assert both["total"] == 2
    assert scoped["total"] == 1
    assert scoped["items"][0]["url"] == "https://example.com/report-two.pdf"


def test_another_workspaces_channel_id_returns_nothing(client: TestClient):
    """Not a 404 — the workspace filter simply matches no rows, so the id
    reveals nothing about whether it exists elsewhere."""
    register_workspace(client, email="chan3@example.com", workspace_name="Chan3")
    other_ws = client.get("/auth/me").json()["workspace_id"]
    foreign = client.post("/channels", json={"tg_channel_id": "9", "username": "theirs"}).json()
    _seed_link(other_ws, foreign["id"], 1, "https://secret.example/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="chan4@example.com", workspace_name="Chan4")
    resp = client.get("/links", params={"channel_id": foreign["id"]})

    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_non_numeric_channel_id_is_rejected(client: TestClient):
    register_workspace(client, email="chan5@example.com", workspace_name="Chan5")

    assert client.get("/links", params={"channel_id": "abc"}).status_code == 422


# --- workspace rename ------------------------------------------------------


def test_workspace_can_be_renamed(client: TestClient):
    register_workspace(client, email="ren@example.com", workspace_name="Old Name")

    resp = client.patch("/auth/workspace", json={"name": "New Name"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert client.get("/auth/me").json()["workspace_name"] == "New Name"


def test_rename_is_recorded_in_the_audit_log(client: TestClient):
    register_workspace(client, email="ren2@example.com", workspace_name="Before")
    client.patch("/auth/workspace", json={"name": "After"})

    entries = [a["detail"] for a in client.get("/auth/me/export").json()["audit_log"]]

    assert "Before -> After" in entries


def test_rename_trims_surrounding_whitespace(client: TestClient):
    register_workspace(client, email="ren3@example.com", workspace_name="Old")

    assert client.patch("/auth/workspace", json={"name": "  Trimmed  "}).json()["name"] == "Trimmed"


def test_blank_rename_is_rejected(client: TestClient):
    register_workspace(client, email="ren4@example.com", workspace_name="Keep This")

    assert client.patch("/auth/workspace", json={"name": "   "}).status_code == 422
    assert client.patch("/auth/workspace", json={"name": ""}).status_code == 422
    assert client.get("/auth/me").json()["workspace_name"] == "Keep This"


def test_rename_only_touches_the_callers_own_workspace(client: TestClient):
    register_workspace(client, email="ren5@example.com", workspace_name="Theirs")
    client.post("/auth/logout")
    register_workspace(client, email="ren6@example.com", workspace_name="Mine")

    client.patch("/auth/workspace", json={"name": "Renamed"})
    client.post("/auth/logout")

    client.post("/auth/login", json={"email": "ren5@example.com", "password": "password123"})
    assert client.get("/auth/me").json()["workspace_name"] == "Theirs"


def test_rename_requires_authentication(client: TestClient):
    assert client.patch("/auth/workspace", json={"name": "x"}).status_code == 401


# --- theme toggle ----------------------------------------------------------


def test_page_ships_all_three_theme_states(client: TestClient):
    """Two states would strand anyone who once picked a theme: they could
    never hand control back to the operating system."""
    body = client.get("/login").text

    assert '"system", "light", "dark"' in body
    assert 'localStorage.removeItem("theme")' in body  # how "system" is stored


def test_theme_is_applied_before_the_body_renders(client: TestClient):
    """Applying it later shows a flash of the wrong palette on every load."""
    body = client.get("/login").text
    head_script = body.split("<style>")[0]

    assert 'localStorage.getItem("theme")' in head_script
    assert 'setAttribute("data-theme"' in head_script


def test_dark_palette_is_defined_for_both_the_os_and_the_explicit_choice(client: TestClient):
    body = client.get("/login").text

    assert "@media (prefers-color-scheme: dark)" in body
    assert ':root:not([data-theme="light"])' in body  # explicit light beats the OS
    assert ':root[data-theme="dark"]' in body  # explicit dark beats the OS


def test_every_colour_token_has_a_light_definition(client: TestClient):
    """A token defined only inside a media query renders as nothing when
    the query does not match."""
    body = client.get("/login").text
    base = body.split("@media")[0]

    for token in ("--bg", "--fg", "--muted", "--border", "--card-bg", "--tag-bg", "--accent", "--danger"):
        assert f"{token}:" in base, f"{token} has no light-mode definition"
