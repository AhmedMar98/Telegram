"""The operator's screen, and the credential decision behind it.

Phase 10. Ideas 181 and 183 ask this service to *read* the GitHub Actions
API, which would mean storing a GitHub token with repo scope in a database
whose whole security model has been built around not holding anything that
powerful. The direction is inverted instead: workflows report in with a
personal API key that cannot touch the repository at all.

That inversion is the thing worth testing — both that it works, and that
the weaker credential really is all it can do.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import metrics
from app.database import SessionLocal
from app.models import WorkflowRun
from tests.conftest import register_workspace


def _key(client: TestClient) -> str:
    return client.post("/auth/api-keys", json={"name": "ci"}).json()["key"]


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- reporting in ----------------------------------------------------------


def test_a_workflow_reports_its_outcome_with_an_api_key(client: TestClient):
    """The whole point of the inversion: CI authenticates with something
    that cannot reach the repository."""
    register_workspace(client, email="st1@example.com", workspace_name="ST1")
    key = _key(client)
    client.cookies.clear()

    response = client.post(
        "/status/workflow-runs",
        json={"name": "collector", "conclusion": "success", "detail": "37 new link(s)"},
        headers=_bearer(key),
    )

    assert response.status_code == 201
    assert response.json()["name"] == "collector"


def test_failures_are_reportable_too(client: TestClient):
    """A board that only ever hears about successes cannot show a failure
    — which is the one thing it exists for."""
    register_workspace(client, email="st2@example.com", workspace_name="ST2")
    key = _key(client)
    client.cookies.clear()

    assert (
        client.post(
            "/status/workflow-runs",
            json={"name": "collector", "conclusion": "failure", "detail": "session revoked"},
            headers=_bearer(key),
        ).status_code
        == 201
    )

    client.post("/auth/login", json={"email": "st2@example.com", "password": "j8Kd0-slwQ2x"})
    runs = client.get("/status").json()["latest_runs"]
    assert runs[0]["conclusion"] == "failure"


def test_the_reporting_key_cannot_do_anything_else(client: TestClient):
    """The credential handed to CI is deliberately the weak one: it writes
    status rows and nothing destructive."""
    register_workspace(client, email="st3@example.com", workspace_name="ST3")
    key = _key(client)
    client.cookies.clear()
    headers = _bearer(key)

    assert client.get("/status", headers=headers).status_code == 403
    assert client.get("/auth/api-keys", headers=headers).status_code == 403
    assert (
        client.post(
            "/auth/me/delete",
            json={"current_password": "j8Kd0-slwQ2x", "confirm": "DELETE"},
            headers=headers,
        ).status_code
        == 403
    )


def test_reporting_needs_some_credential(client: TestClient):
    assert client.post("/status/workflow-runs", json={"name": "x", "conclusion": "success"}).status_code == 401


def test_only_the_newest_run_per_workflow_is_shown(client: TestClient):
    """The board answers "is it healthy now", not "what happened all
    year" — and the table grows with every scheduled run forever."""
    register_workspace(client, email="st4@example.com", workspace_name="ST4")
    key = _key(client)
    client.cookies.clear()

    for conclusion in ("failure", "failure", "success"):
        client.post(
            "/status/workflow-runs",
            json={"name": "vitality", "conclusion": conclusion},
            headers=_bearer(key),
        )
    client.post("/status/workflow-runs", json={"name": "backup", "conclusion": "success"}, headers=_bearer(key))

    client.post("/auth/login", json={"email": "st4@example.com", "password": "j8Kd0-slwQ2x"})
    runs = client.get("/status").json()["latest_runs"]

    assert {r["name"] for r in runs} == {"vitality", "backup"}
    vitality = next(r for r in runs if r["name"] == "vitality")
    assert vitality["conclusion"] == "success"

    with SessionLocal() as db:
        assert db.query(WorkflowRun).count() == 4, "history is kept; only the view is narrowed"


def test_one_workspace_never_sees_anothers_runs(client: TestClient):
    register_workspace(client, email="st5a@example.com", workspace_name="ST5A")
    key = _key(client)
    client.cookies.clear()
    client.post(
        "/status/workflow-runs",
        json={"name": "secret-job", "conclusion": "success"},
        headers=_bearer(key),
    )

    register_workspace(client, email="st5b@example.com", workspace_name="ST5B")
    body = client.get("/status").text

    assert "secret-job" not in body
    assert client.get("/status").json()["latest_runs"] == []


# --- the counters ----------------------------------------------------------


def test_request_timings_are_recorded(client: TestClient):
    register_workspace(client, email="st6@example.com", workspace_name="ST6")
    metrics.reset()

    for _ in range(3):
        client.get("/links")

    body = client.get("/status").json()

    assert body["requests_since_start"] >= 3
    assert body["sampled_requests"] >= 3
    assert body["median_response_ms"] >= 0


def test_only_server_errors_count_as_errors(client: TestClient):
    """A 404 or a 422 is the API working as designed. Counting those would
    make the number meaningless."""
    register_workspace(client, email="st7@example.com", workspace_name="ST7")
    metrics.reset()

    client.get("/links/999999")  # 404
    client.get("/links", params={"favorite": "maybe"})  # 422

    assert client.get("/status").json()["server_errors_since_start"] == 0


def test_the_counters_are_bounded_by_construction():
    """An unbounded list of every request duration is the classic version
    of this that quietly becomes the outage it was meant to detect."""
    metrics.reset()

    for _ in range(metrics.WINDOW * 3):
        metrics.record(0.01, status_code=200)

    snapshot = metrics.snapshot()
    assert snapshot["requests_since_start"] == metrics.WINDOW * 3
    assert snapshot["sampled_requests"] == metrics.WINDOW


def test_percentiles_reflect_the_distribution():
    metrics.reset()
    for _ in range(99):
        metrics.record(0.01, status_code=200)
    metrics.record(1.0, status_code=200)

    snapshot = metrics.snapshot()

    assert snapshot["median_response_ms"] == 10.0
    assert snapshot["slowest_response_ms"] == 1000.0
    assert snapshot["p95_response_ms"] >= 10.0


def test_the_status_screen_needs_a_session(client: TestClient):
    assert client.get("/status").status_code == 401


def test_deploy_identity_is_reported_when_the_platform_supplies_it(client: TestClient, monkeypatch):
    """Idea 187. Render sets these on every build, so nothing has to be
    bumped by hand at release time."""
    from app.config import get_settings

    register_workspace(client, email="st8@example.com", workspace_name="ST8")

    monkeypatch.setenv("RENDER_GIT_COMMIT", "d97841c733f29358f242798e89270a389ca5201b")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "link-intel-web")
    get_settings.cache_clear()
    try:
        body = client.get("/status").json()
        assert body["deploy_commit"].startswith("d97841c")
        assert body["service_name"] == "link-intel-web"
    finally:
        get_settings.cache_clear()


def test_an_absent_deploy_commit_reads_as_unknown_not_as_a_guess(client: TestClient):
    """Locally there is no Render build, and inventing a version string
    would be worse than saying nothing."""
    register_workspace(client, email="st9@example.com", workspace_name="ST9")

    assert client.get("/status").json()["deploy_commit"] is None
