"""An export must carry links out, and nothing else.

§21 and §28 forbid a export from containing passwords, session strings,
TOTP secrets, database credentials, encryption keys, bot tokens, API
secrets or webhook secrets. Nothing checked. The three link exports build
their rows from ``app.routers.links._export_row``, which reads only
``Link`` columns, so today they are clean — but "the function currently
only touches one model" is an observation about the present, and an export
is exactly the surface where a convenience column added later ("show me
which account collected this") walks a credential out of the database in a
file the user then emails to somebody.

So this file guards it two ways, because either alone is escapable:

* **By value.** The workspace is seeded with real secret-shaped strings in
  every column the platform stores one in, and the exports are searched
  for them. Catches a leak however it arrives — a new column, a join, a
  serialiser change.

* **By shape.** The export's column set is pinned to a reviewed list, so
  adding a field is a decision somebody makes on purpose rather than a
  diff nobody read. Catches a leak the value test would miss because the
  secret in production does not look like the fixture's.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.crypto import encrypt_field
from app.database import SessionLocal
from app.models import ApiKey, TelegramAccount, User, Workspace
from tests.conftest import register_workspace

# Distinctive enough that a substring match cannot fire by accident, and
# shaped like the real thing in each case.
SESSION_STRING = "1BVtsOHUBu0canary0session0string0must0never0be0exported"
WEBHOOK_URL = "https://hooks.example.com/services/T000/B000/canary0webhook0token"
API_KEY_HASH = "canary" + "0" * 58
TOTP_SECRET = "CANARYTOTPSECRETMUSTNOTLEAK"

SECRETS = (SESSION_STRING, WEBHOOK_URL, API_KEY_HASH, TOTP_SECRET)

# Every field the link exports are allowed to carry. Reviewed as a list of
# facts about a *link*: where it points, how it was classified, when it was
# seen and whether it still works. Nothing here identifies a credential, an
# account, or a user.
EXPECTED_EXPORT_COLUMNS = {
    "url",
    "category",
    "confidence",
    "classified_by",
    "matched_rule",
    "source_type",
    "forwarded_from",
    "language",
    "is_favorite",
    "domain",
    "posted_at",
    "collected_at",
    "is_alive",
    "status_category",
    "http_status",
    "last_checked_at",
    "last_alive_at",
    "is_archived",
    "context",
}


@pytest.fixture
def workspace_with_secrets(client: TestClient) -> TestClient:
    """A workspace holding a real secret in every column that stores one."""
    register_workspace(client, email="canary@example.com", workspace_name="Canary Co")
    client.post("/channels", json={"tg_channel_id": "-100555", "username": "canary_src"})
    client.post("/links", json={"text": "https://example.com/exported.pdf"})

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "canary@example.com").one()
        workspace = db.get(Workspace, user.workspace_id)
        # Encrypted at rest, exactly as the application stores it — so this
        # tests the real path rather than a plaintext stand-in that the
        # export could never have reached anyway.
        workspace.webhook_url = encrypt_field(WEBHOOK_URL)
        user.totp_secret = encrypt_field(TOTP_SECRET)
        db.add(
            TelegramAccount(
                workspace_id=workspace.id,
                label="canary account",
                session_string=encrypt_field(SESSION_STRING),
            )
        )
        db.add(
            ApiKey(
                workspace_id=workspace.id,
                user_id=user.id,
                name="canary key",
                token_hash=API_KEY_HASH,
                prefix="canary01",
            )
        )
        db.commit()
    finally:
        db.close()
    return client


@pytest.mark.parametrize("path", ["/links/export.csv", "/links/export.json", "/links/export.md"])
def test_no_export_format_contains_any_stored_secret(workspace_with_secrets, path: str):
    """Searched as raw text, so an escaped or re-encoded copy still counts."""
    body = workspace_with_secrets.get(path).text

    assert "exported.pdf" in body, f"{path} returned nothing, so finding no secret proves nothing"
    for secret in SECRETS:
        assert secret not in body, f"{path} leaked a stored secret"


@pytest.mark.parametrize("path", ["/links/export.csv", "/links/export.json", "/links/export.md"])
def test_no_export_format_names_a_credential_field(workspace_with_secrets, path: str):
    """A column whose *name* is a credential is a leak already half made.

    Catches the shape the value test cannot: a field exported as an empty
    string or a mask still says the platform holds one, and the next
    change that fills it in will not be reviewed as a security change.
    """
    body = workspace_with_secrets.get(path).text.lower()

    for word in ("session_string", "password", "token_hash", "totp", "webhook", "secret", "api_key"):
        assert word not in body, f"{path} names a credential field"


def test_the_export_columns_are_exactly_the_reviewed_set(workspace_with_secrets):
    """Guards the guard: a new export column must be decided, not merged.

    The value test above only catches a secret that looks like the
    fixture's. This one catches any field arriving at all, which is the
    check that still works when the leak is a column nobody thought of.
    """
    from app.routers.links import EXPORT_COLUMNS

    assert set(EXPORT_COLUMNS) == EXPECTED_EXPORT_COLUMNS, (
        "the link export's fields changed. Each addition walks out of the "
        "database in a file users share — review it as a disclosure, then "
        "update EXPECTED_EXPORT_COLUMNS."
    )

    header = next(csv.reader(io.StringIO(workspace_with_secrets.get("/links/export.csv").text)))
    assert set(header) == EXPECTED_EXPORT_COLUMNS, "the CSV header drifted from EXPORT_COLUMNS"

    rows = workspace_with_secrets.get("/links/export.json").json()
    assert set(rows[0]) == EXPECTED_EXPORT_COLUMNS, "the JSON rows drifted from EXPORT_COLUMNS"


def test_the_whole_workspace_export_still_withholds_credentials(workspace_with_secrets):
    """The portability export is wider in scope and no looser about this.

    It is the endpoint that hands back accounts and users rather than only
    links, so it is the one where a credential would be least surprising
    and most damaging.
    """
    body = workspace_with_secrets.get("/auth/me/export").text

    assert "exported.pdf" in body
    for secret in SECRETS:
        assert secret not in body, "the workspace export leaked a stored secret"
