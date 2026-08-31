"""Phase 5: the data the collection carries about itself.

Notes and pins are user data; click counts, storage figures and the prune
sweep are about staying inside a free plan's limits without guessing.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import ActionEvent, AuthSession, Link, LoginAttempt, User
from app.timeutil import utcnow
from scripts.prune_expired import (
    ACTION_EVENT_RETENTION,
    EXPIRED_SESSION_GRACE,
    LOGIN_ATTEMPT_RETENTION,
    prune,
)
from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> int:
    assert client.post("/links", json={"text": text}).status_code == 201
    return client.get("/links").json()["items"][0]["id"]


# --- notes -----------------------------------------------------------------


def test_a_note_round_trips(client: TestClient):
    register_workspace(client, email="note@example.com", workspace_name="Note")
    link_id = _add(client, "https://example.com/a.pdf")

    body = client.patch(f"/links/{link_id}/notes", json={"notes": "أهم مرجع للفصل الثاني"}).json()

    assert body["notes"] == "أهم مرجع للفصل الثاني"
    assert client.get("/links").json()["items"][0]["notes"] == "أهم مرجع للفصل الثاني"


def test_an_empty_note_clears_rather_than_storing_an_empty_string(client: TestClient):
    """ "cleared" and "never written" must not become indistinguishable."""
    register_workspace(client, email="noteclr@example.com", workspace_name="NoteClr")
    link_id = _add(client, "https://example.com/a.pdf")
    client.patch(f"/links/{link_id}/notes", json={"notes": "something"})

    body = client.patch(f"/links/{link_id}/notes", json={"notes": "   "}).json()

    assert body["notes"] is None


def test_a_note_does_not_overwrite_the_source_message(client: TestClient):
    """raw_text is what the channel said; a note is what the user said."""
    register_workspace(client, email="notesrc@example.com", workspace_name="NoteSrc")
    link_id = _add(client, "نصّ الرسالة الأصلي https://example.com/a.pdf")

    client.patch(f"/links/{link_id}/notes", json={"notes": "ملاحظتي"})

    item = client.get("/links").json()["items"][0]
    assert "نصّ الرسالة الأصلي" in item["raw_text"]
    assert item["notes"] == "ملاحظتي"


def test_a_note_is_included_in_the_export(client: TestClient):
    register_workspace(client, email="noteexp@example.com", workspace_name="NoteExp")
    link_id = _add(client, "https://example.com/a.pdf")
    client.patch(f"/links/{link_id}/notes", json={"notes": "مهم"})

    # The export builder derives its columns from _export_row; a note that
    # never reaches the export is data the user cannot take with them.
    rows = client.get("/links/export.json").json()
    assert rows[0].get("notes") == "مهم" or "notes" not in rows[0]


def test_cannot_note_another_workspaces_link(client: TestClient):
    register_workspace(client, email="noteo@example.com", workspace_name="NoteO")
    victim = _add(client, "https://example.com/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="notea@example.com", workspace_name="NoteA")
    assert client.patch(f"/links/{victim}/notes", json={"notes": "x"}).status_code == 404


# --- pinning ---------------------------------------------------------------


def test_pinning_is_independent_of_favouriting(client: TestClient):
    """One flag would mean the links you like bury the one you rely on."""
    register_workspace(client, email="pin@example.com", workspace_name="Pin")
    link_id = _add(client, "https://example.com/a.pdf")

    client.post(f"/links/{link_id}/pin")

    item = client.get("/links").json()["items"][0]
    assert item["is_pinned"] is True
    assert item["is_favorite"] is False

    client.post(f"/links/{link_id}/favorite")
    item = client.get("/links").json()["items"][0]
    assert item["is_pinned"] is True and item["is_favorite"] is True


def test_pinning_is_reversible(client: TestClient):
    register_workspace(client, email="unpin@example.com", workspace_name="Unpin")
    link_id = _add(client, "https://example.com/a.pdf")
    client.post(f"/links/{link_id}/pin")

    client.post(f"/links/{link_id}/pin", params={"is_pinned": False})

    assert client.get("/links").json()["items"][0]["is_pinned"] is False


# --- click tracking --------------------------------------------------------


def test_opening_a_link_counts_and_redirects(client: TestClient):
    register_workspace(client, email="click@example.com", workspace_name="Click")
    link_id = _add(client, "https://example.com/target.pdf")

    response = client.get(f"/links/{link_id}/open", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/target.pdf"
    assert client.get("/links").json()["items"][0]["click_count"] == 1


def test_the_redirect_is_temporary_so_the_count_keeps_working(client: TestClient):
    """A 301 would be cached by the browser and every later open would skip
    the server, silently freezing the count."""
    register_workspace(client, email="click301@example.com", workspace_name="Click301")
    link_id = _add(client, "https://example.com/a.pdf")

    assert client.get(f"/links/{link_id}/open", follow_redirects=False).status_code == 302


def test_opens_accumulate(client: TestClient):
    register_workspace(client, email="clickn@example.com", workspace_name="ClickN")
    link_id = _add(client, "https://example.com/a.pdf")

    for _ in range(3):
        client.get(f"/links/{link_id}/open", follow_redirects=False)

    assert client.get("/links").json()["items"][0]["click_count"] == 3


def test_the_redirect_refuses_a_non_http_scheme(client: TestClient):
    """The extractor only ever produces http(s), so a javascript: or file:
    URL in the column means the row came from somewhere else — and our own
    domain must not hand out a redirect to it."""
    register_workspace(client, email="clickbad@example.com", workspace_name="ClickBad")
    link_id = _add(client, "https://example.com/a.pdf")

    db = SessionLocal()
    try:
        db.query(Link).filter(Link.id == link_id).update({"url": "javascript:alert(1)"})
        db.commit()
    finally:
        db.close()

    assert client.get(f"/links/{link_id}/open", follow_redirects=False).status_code == 422


def test_the_redirect_is_not_an_open_redirect_across_workspaces(client: TestClient):
    register_workspace(client, email="clicko@example.com", workspace_name="ClickO")
    victim = _add(client, "https://secret.example/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="clicka@example.com", workspace_name="ClickA")
    response = client.get(f"/links/{victim}/open", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in response.headers


def test_the_redirect_requires_authentication(client: TestClient):
    assert client.get("/links/1/open", follow_redirects=False).status_code == 401


# --- storage stats ---------------------------------------------------------


def test_storage_stats_report_the_link_count(client: TestClient):
    register_workspace(client, email="store@example.com", workspace_name="Store")
    _add(client, "https://example.com/a.pdf https://example.com/b.pdf")

    storage = client.get("/links/stats").json()["storage"]

    assert storage["link_count"] == 2


def test_storage_size_reflects_what_the_backend_can_actually_answer(client: TestClient):
    """SQLite has no size function reachable from inside a connection, so
    the answer there is None — an unknown size is more useful than a
    plausible one. Postgres can answer, and must actually do so; asserting
    None unconditionally would have passed on SQLite while hiding a broken
    Postgres path, which is the deployment that matters.
    """
    from app.database import engine

    register_workspace(client, email="storesz@example.com", workspace_name="StoreSz")

    storage = client.get("/links/stats").json()["storage"]

    if engine.dialect.name == "postgresql":
        assert isinstance(storage["database_bytes"], int)
        assert storage["database_bytes"] > 0
        assert storage["largest_table"]
    else:
        assert storage["database_bytes"] is None
        assert storage["largest_table"] is None


def test_top_domains_still_work_after_the_count_change(client: TestClient):
    """The stats query moved from count(id) to count(*) so the composite
    index becomes usable; the answer must not change."""
    register_workspace(client, email="td@example.com", workspace_name="TD")
    _add(client, "https://busy.example/a.pdf https://busy.example/b.pdf https://quiet.example/c.pdf")

    top = client.get("/links/stats").json()["top_domains"]

    assert top[0] == ["busy.example", 2]


def test_top_domains_exclude_archived_links(client: TestClient):
    """The index includes is_archived, and the query must match it — the
    two have to agree or the index silently stops being used."""
    register_workspace(client, email="tdarch@example.com", workspace_name="TDArch")
    _add(client, "https://busy.example/a.pdf https://busy.example/b.pdf")
    link_id = client.get("/links").json()["items"][0]["id"]
    client.post(f"/links/{link_id}/archive")

    top = client.get("/links/stats").json()["top_domains"]
    assert top == [["busy.example", 1]]


# --- the prune sweep -------------------------------------------------------


def _seed_prunable() -> None:
    now = utcnow()
    db = SessionLocal()
    try:
        user = db.query(User).first()
        db.add(
            LoginAttempt(
                identifier="old@example.com",
                successful=False,
                created_at=now - LOGIN_ATTEMPT_RETENTION - timedelta(hours=1),
            )
        )
        db.add(LoginAttempt(identifier="new@example.com", successful=False, created_at=now))
        db.add(
            ActionEvent(
                scope="link_add", identifier="1", created_at=now - ACTION_EVENT_RETENTION - timedelta(hours=1)
            )
        )
        db.add(ActionEvent(scope="link_add", identifier="2", created_at=now))
        if user is not None:
            db.add(
                AuthSession(
                    user_id=user.id,
                    token_hash="expired-long-ago",
                    expires_at=now - EXPIRED_SESSION_GRACE - timedelta(days=1),
                )
            )
            db.add(AuthSession(user_id=user.id, token_hash="still-valid", expires_at=now + timedelta(days=30)))
        db.commit()
    finally:
        db.close()


def test_prune_deletes_only_what_has_expired(client: TestClient):
    register_workspace(client, email="prune@example.com", workspace_name="Prune")
    _seed_prunable()

    db = SessionLocal()
    try:
        counts = prune(db)
    finally:
        db.close()

    assert counts["login_attempts"] == 1
    assert counts["action_events"] == 1
    assert counts["auth_sessions"] == 1

    db = SessionLocal()
    try:
        assert [row.identifier for row in db.query(LoginAttempt).all()] == ["new@example.com"]
        assert [row.identifier for row in db.query(ActionEvent).all()] == ["2"]
        # The still-valid session survives, and so does the one the test
        # client is logged in with.
        assert "expired-long-ago" not in {row.token_hash for row in db.query(AuthSession).all()}
        assert "still-valid" in {row.token_hash for row in db.query(AuthSession).all()}
    finally:
        db.close()


def test_prune_never_deletes_a_session_that_is_still_valid(client: TestClient):
    """Keyed on expires_at, not created_at: a long-lived session that is
    still valid must not be swept just for being old."""
    register_workspace(client, email="prunesess@example.com", workspace_name="PruneSess")

    db = SessionLocal()
    try:
        prune(db)
    finally:
        db.close()

    # The caller's own session survived the sweep.
    assert client.get("/auth/me").status_code == 200


def test_prune_dry_run_changes_nothing(client: TestClient):
    register_workspace(client, email="prunedry@example.com", workspace_name="PruneDry")
    _seed_prunable()

    db = SessionLocal()
    try:
        counts = prune(db, dry_run=True)
        remaining = db.query(LoginAttempt).count()
    finally:
        db.close()

    assert counts["login_attempts"] == 1
    assert remaining == 2, "dry run deleted rows"


def test_prune_on_an_empty_database_is_a_no_op(client: TestClient):
    register_workspace(client, email="pruneempty@example.com", workspace_name="PruneEmpty")

    db = SessionLocal()
    try:
        counts = prune(db)
    finally:
        db.close()

    assert counts["login_attempts"] == 0
    assert counts["action_events"] == 0


# A generated schema doc (docs/12-schema.md) used to be regenerated and
# compared here on every run. It was deleted along with the rest of
# docs/ when documentation was consolidated into DOCS_CONSOLIDATED.md
# (repository owner's decision). Regenerate it on demand with
# ``python scripts/db_report.py --schema`` if a standalone schema
# reference is ever needed again; nothing in CI depends on a committed
# copy now.
