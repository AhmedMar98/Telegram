from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import register_workspace


def test_add_duplicate_channel_rejected(client: TestClient):
    register_workspace(client, email="ch@example.com", workspace_name="Chans")
    first = client.post("/channels", json={"tg_channel_id": "42", "username": "c"})
    assert first.status_code == 201
    dup = client.post("/channels", json={"tg_channel_id": "42", "username": "c"})
    assert dup.status_code == 409


def test_deactivate_channel(client: TestClient):
    register_workspace(client, email="ch2@example.com", workspace_name="Chans2")
    channel = client.post("/channels", json={"tg_channel_id": "43", "username": "c2"}).json()

    delete = client.delete(f"/channels/{channel['id']}")
    assert delete.status_code == 204

    channels = client.get("/channels").json()
    assert channels[0]["is_active"] is False


def test_deactivate_missing_channel_404(client: TestClient):
    register_workspace(client, email="ch3@example.com", workspace_name="Chans3")
    resp = client.delete("/channels/999999")
    assert resp.status_code == 404


def test_bot_link_code_requires_auth(client: TestClient):
    assert client.post("/bot/link-code").status_code == 401


def test_bot_link_code_issued_when_authenticated(client: TestClient):
    register_workspace(client, email="bot@example.com", workspace_name="BotCo")
    resp = client.post("/bot/link-code")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["code"]) == 8
    assert body["code"] in body["instructions"]


def test_webhook_rejects_wrong_secret(client: TestClient):
    resp = client.post("/telegram/webhook/wrong-secret", json={})
    assert resp.status_code == 404


def test_dashboard_redirects_when_not_authenticated(client: TestClient):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_index_redirects_to_login_when_anonymous(client: TestClient):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_healthz(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_database_reachable(client: TestClient):
    """Readiness must not depend on the migration table existing.

    The absence of ``alembic_version`` is asserted by removing it first
    rather than assuming it: conftest's reset drops only tables on
    ``Base.metadata``, so a leftover from another test would otherwise
    decide this test's outcome by execution order.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
        db.commit()
    finally:
        db.close()

    body = client.get("/readyz").json()

    assert body["status"] == "ready"
    # A database built by create_all() rather than alembic has no revision
    # to report. None is the honest answer, not an error.
    assert body["schema_version"] is None


def test_readyz_reports_the_applied_migration_when_there_is_one(client: TestClient):
    """On a real deployment the schema is owned by alembic, and the point
    of the field is diagnosing a half-applied migration.

    The table is dropped again afterwards. conftest's reset drops only
    tables registered on ``Base.metadata``, and ``alembic_version`` is not
    one of them — leaving it behind made the test above pass or fail
    depending on execution order, which showed up as a Postgres-only
    failure the SQLite run never saw.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        db.execute(text("DELETE FROM alembic_version"))
        db.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0012_notes_pins_clicks')"))
        db.commit()
    finally:
        db.close()

    try:
        assert client.get("/readyz").json()["schema_version"] == "0012_notes_pins_clicks"
    finally:
        db = SessionLocal()
        try:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))
            db.commit()
        finally:
            db.close()


def test_healthz_still_does_not_touch_the_database():
    """Liveness must stay database-free: Render restarts the service when
    it fails, so a transient database blip would become a restart loop."""
    import inspect

    from app.main import healthz

    source = inspect.getsource(healthz)
    assert "db" not in inspect.signature(healthz).parameters
    assert "execute" not in source
