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
