"""Human corrections to the classifier, kept as a durable signal.

A correction used to be a silent mutation: the link's category changed and
what the classifier had said was gone. That is the one piece of data worth
keeping, because it is the concrete list of what the rules get wrong.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import ClassificationFeedback, Link
from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> int:
    assert client.post("/links", json={"text": text}).status_code == 201
    return client.get("/links").json()["items"][0]["id"]


def test_a_correction_is_recorded_with_what_the_classifier_said(client: TestClient):
    register_workspace(client, email="fb@example.com", workspace_name="FB")
    link_id = _add(client, "https://example.com/thing.apk")

    before = client.get("/links").json()["items"][0]
    assert before["category"] == "software_apps"

    assert client.patch(f"/links/{link_id}", json={"category": "games"}).status_code == 200

    items = client.get("/links/feedback").json()["items"]
    assert len(items) == 1
    entry = items[0]
    assert entry["link_id"] == link_id
    assert entry["url"] == "https://example.com/thing.apk"
    assert entry["previous_category"] == "software_apps"
    assert entry["new_category"] == "games"
    # The classifier's own reasoning is preserved, which is the whole point:
    # without it the correction says what was wrong but not why.
    assert entry["previous_matched_rule"] == before["matched_rule"]
    assert entry["previous_confidence"] == before["confidence"]


def test_recategorizing_to_the_same_category_records_nothing(client: TestClient):
    """Re-selecting the category that is already set is not a correction."""
    register_workspace(client, email="fbsame@example.com", workspace_name="FBSame")
    link_id = _add(client, "https://example.com/thing.apk")

    client.patch(f"/links/{link_id}", json={"category": "software_apps"})

    assert client.get("/links/feedback").json()["total"] == 0


def test_successive_corrections_build_a_history(client: TestClient):
    register_workspace(client, email="fbhist@example.com", workspace_name="FBHist")
    link_id = _add(client, "https://example.com/thing.apk")

    client.patch(f"/links/{link_id}", json={"category": "games"})
    client.patch(f"/links/{link_id}", json={"category": "movies_series"})

    items = client.get("/links/feedback", params={"link_id": link_id}).json()["items"]
    assert [(i["previous_category"], i["new_category"]) for i in items] == [
        ("games", "movies_series"),
        ("software_apps", "games"),
    ]
    # The second correction records confidence 1.0 — the value the *first*
    # correction set — not the classifier's original score. That is correct:
    # it describes the state that was overruled, whatever produced it.
    assert items[0]["previous_confidence"] == 1.0


def test_feedback_survives_deleting_the_link(client: TestClient):
    """The most useful correction to learn from is often on a link that was
    later deleted; losing it with the link throws away the signal."""
    register_workspace(client, email="fbdel@example.com", workspace_name="FBDel")
    link_id = _add(client, "https://example.com/thing.apk")
    client.patch(f"/links/{link_id}", json={"category": "games"})

    assert client.delete(f"/links/{link_id}").status_code == 204

    assert client.get("/links").json()["total"] == 0
    assert client.get("/links/feedback").json()["total"] == 1


def test_bulk_recategorize_does_not_flood_the_feedback_log(client: TestClient):
    """Sweeping a filter is a reorganization, not a judgement about each
    link; recording thousands of swept rows would bury the real signal."""
    register_workspace(client, email="fbbulk@example.com", workspace_name="FBBulk")
    _add(client, "https://example.com/a.apk https://example.com/b.exe")

    resp = client.post("/links/bulk/recategorize", json={"category": "software_apps", "new_category": "games"})
    assert resp.json()["affected"] == 2

    assert client.get("/links/feedback").json()["total"] == 0


def test_feedback_is_scoped_to_the_workspace(client: TestClient):
    register_workspace(client, email="fbiso1@example.com", workspace_name="FBIso1")
    mine = _add(client, "https://example.com/mine.apk")
    client.patch(f"/links/{mine}", json={"category": "games"})
    client.post("/auth/logout")

    register_workspace(client, email="fbiso2@example.com", workspace_name="FBIso2")
    assert client.get("/links/feedback").json()["total"] == 0
    # Asking for another workspace's link id returns an empty list, not a
    # 403 — a 403 would confirm the id exists.
    resp = client.get("/links/feedback", params={"link_id": mine})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_feedback_requires_authentication(client: TestClient):
    assert client.get("/links/feedback").status_code == 401


def test_feedback_respects_the_limit(client: TestClient):
    register_workspace(client, email="fblim@example.com", workspace_name="FBLim")
    link_id = _add(client, "https://example.com/thing.apk")
    for category in ("games", "movies_series", "books_courses"):
        client.patch(f"/links/{link_id}", json={"category": category})

    body = client.get("/links/feedback", params={"limit": 2}).json()
    assert body["total"] == 3  # the true count, not the page size
    assert len(body["items"]) == 2


def test_deleting_the_workspace_removes_its_feedback(client: TestClient):
    """Feedback outliving a link is deliberate; outliving the workspace that
    owns it would be a data-retention bug."""
    register_workspace(client, email="fbwipe@example.com", workspace_name="FBWipe")
    link_id = _add(client, "https://example.com/thing.apk")
    client.patch(f"/links/{link_id}", json={"category": "games"})

    resp = client.post("/auth/me/delete", json={"current_password": "j8Kd0-slwQ2x", "confirm": "DELETE"})
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        assert db.query(ClassificationFeedback).count() == 0
        assert db.query(Link).count() == 0
    finally:
        db.close()
