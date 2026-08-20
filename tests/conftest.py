import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ENVIRONMENT", "test")
# Key stretching is the point in production and pure waste in tests.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


def _assert_databases_are_separate() -> None:
    """Refuse to run if DATABASE_URL and PG_TEST_DSN name the same database.

    ``_reset_db`` below drops every table after each test. The Postgres
    search tests seed their data once per *module*, so pointing both
    variables at one database lets that teardown delete the seed rows and
    the GIN index out from under them. The result is eight silent
    "search found nothing" failures plus a "planner ignored the index"
    failure — which read exactly like a real full-text-search regression
    and send you hunting through app/search.py instead of your shell.

    Setting both is a natural thing to try ("run the whole suite against
    real Postgres"), so this fails loudly and immediately instead.
    """
    app_db = os.environ.get("DATABASE_URL", "")
    pg_test = os.environ.get("PG_TEST_DSN", "")
    if pg_test and app_db and app_db == pg_test:
        raise pytest.UsageError(
            "DATABASE_URL and PG_TEST_DSN point at the same database "
            f"({app_db!r}). The per-test teardown would wipe the Postgres "
            "search fixtures. Use a separate database for PG_TEST_DSN, or "
            "leave DATABASE_URL on SQLite as CI does."
        )


_assert_databases_are_separate()


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def register_workspace(
    client: TestClient, *, email: str, workspace_name: str, password: str = "password123"
) -> TestClient:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": password, "workspace_name": workspace_name},
    )
    assert response.status_code == 201, response.text
    return client
