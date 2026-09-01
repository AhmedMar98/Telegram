"""API surface: the things a client needs that prose cannot provide.

The theme is machine-readability. A caller should be able to branch on a
rejection, know when to retry, fetch a known id directly, and skip a body
that has not changed — none of which should require parsing English.
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import schemas
from app.database import SessionLocal
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.models import TelegramAccount
from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> int:
    assert client.post("/links", json={"text": text}).status_code == 201
    return client.get("/links").json()["items"][0]["id"]


# --- single-resource endpoints --------------------------------------------


def test_a_link_can_be_fetched_by_id(client: TestClient):
    register_workspace(client, email="one@example.com", workspace_name="One")
    link_id = _add(client, "كتاب https://example.com/a.pdf")

    body = client.get(f"/links/{link_id}").json()

    assert body["id"] == link_id
    assert body["url"] == "https://example.com/a.pdf"
    assert body["category"] == "books_courses"


def test_fetching_another_workspaces_link_is_404(client: TestClient):
    register_workspace(client, email="oneo@example.com", workspace_name="OneO")
    victim = _add(client, "https://example.com/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="onea@example.com", workspace_name="OneA")
    assert client.get(f"/links/{victim}").status_code == 404


def test_the_literal_export_paths_still_resolve(client: TestClient):
    """Regression guard. Adding GET /links/{link_id} with a bare path
    parameter swallowed /links/export.csv, /links/saved and every other
    literal path declared after it — thirty tests at once. The int
    converter makes the match structural rather than order-dependent."""
    register_workspace(client, email="paths@example.com", workspace_name="Paths")

    for path in (
        "/links/export.csv",
        "/links/export.json",
        "/links/export.md",
        "/links/saved",
        "/links/random",
        "/links/feedback",
        "/links/stats",
    ):
        assert client.get(path).status_code == 200, f"{path} was shadowed"


def test_a_non_numeric_link_id_is_not_treated_as_an_id(client: TestClient):
    register_workspace(client, email="nonnum@example.com", workspace_name="NonNum")
    assert client.get("/links/not-a-number").status_code == 404


def test_a_channel_can_be_fetched_by_id(client: TestClient):
    register_workspace(client, email="ch1@example.com", workspace_name="Ch1")
    channel = client.post("/channels", json={"tg_channel_id": "42", "username": "test"}).json()

    body = client.get(f"/channels/{channel['id']}").json()

    assert body["id"] == channel["id"]
    assert body["tg_channel_id"] == "42"


def test_the_accounts_path_is_not_shadowed_by_the_channel_id_route(client: TestClient):
    register_workspace(client, email="chacc@example.com", workspace_name="ChAcc")
    assert client.get("/channels/accounts").status_code == 200


def test_fetching_another_workspaces_channel_is_404(client: TestClient):
    register_workspace(client, email="cho@example.com", workspace_name="ChO")
    victim = client.post("/channels", json={"tg_channel_id": "9"}).json()["id"]
    client.post("/auth/logout")

    register_workspace(client, email="cha@example.com", workspace_name="ChA")
    assert client.get(f"/channels/{victim}").status_code == 404


# --- machine-readable rejections ------------------------------------------


def test_an_unknown_category_carries_a_stable_code(client: TestClient):
    """A client should not have to match English to tell rejections apart."""
    register_workspace(client, email="code1@example.com", workspace_name="Code1")
    link_id = _add(client, "https://example.com/a.pdf")

    response = client.patch(f"/links/{link_id}", json={"category": "nope"})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.UNKNOWN_CATEGORY
    # The prose is still there for a human, and still a plain string, so
    # every existing client keeps working.
    assert isinstance(response.json()["detail"], str)


def test_an_unknown_sort_carries_its_own_code(client: TestClient):
    register_workspace(client, email="code2@example.com", workspace_name="Code2")

    response = client.get("/links", params={"sort": "sideways"})

    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.UNKNOWN_SORT


def test_an_unknown_saved_filter_carries_its_own_code(client: TestClient):
    register_workspace(client, email="code3@example.com", workspace_name="Code3")

    response = client.post("/links/saved", json={"name": "x", "filters": {"page_size": "9999"}})

    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.UNKNOWN_FILTER


def test_the_codes_are_distinct(client: TestClient):
    """Two rejections sharing a code would make the code useless."""
    register_workspace(client, email="code4@example.com", workspace_name="Code4")
    link_id = _add(client, "https://example.com/a.pdf")

    category = client.patch(f"/links/{link_id}", json={"category": "nope"}).headers[ERROR_CODE_HEADER]
    sort = client.get("/links", params={"sort": "sideways"}).headers[ERROR_CODE_HEADER]

    assert category != sort


# --- Retry-After -----------------------------------------------------------


def test_a_throttled_submission_says_when_to_come_back(client: TestClient, monkeypatch):
    """A 429 with no Retry-After leaves the client guessing, and the usual
    guess is 'immediately' — exactly what the throttle exists to stop."""
    from app.routers import links as links_module

    monkeypatch.setattr(links_module, "LINK_ADD_LIMIT", 1)
    register_workspace(client, email="retry@example.com", workspace_name="Retry")
    client.post("/links", json={"text": "https://example.com/a.apk"})

    response = client.post("/links", json={"text": "https://example.com/b.apk"})

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.RATE_LIMITED


def test_the_retry_after_matches_the_configured_window(client: TestClient, monkeypatch):
    """The window is a known constant, so the value is a fact rather than
    an estimate — and must not drift from the constant it describes."""
    from app.routers import links as links_module

    monkeypatch.setattr(links_module, "LINK_ADD_LIMIT", 1)
    register_workspace(client, email="retryw@example.com", workspace_name="RetryW")
    client.post("/links", json={"text": "https://example.com/a.apk"})

    response = client.post("/links", json={"text": "https://example.com/b.apk"})

    assert int(response.headers["Retry-After"]) == links_module.LINK_ADD_WINDOW_MINUTES * 60


def test_a_locked_out_login_also_says_when_to_come_back(client: TestClient):
    from app.security import LOGIN_MAX_FAILURES, LOGIN_WINDOW_MINUTES

    register_workspace(client, email="lock@example.com", workspace_name="Lock")
    client.post("/auth/logout")

    for _ in range(LOGIN_MAX_FAILURES + 1):
        client.post("/auth/login", json={"email": "lock@example.com", "password": "wrong-password"})

    response = client.post("/auth/login", json={"email": "lock@example.com", "password": "wrong-password"})

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) == LOGIN_WINDOW_MINUTES * 60
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.LOGIN_THROTTLED


# --- X-Total-Count ---------------------------------------------------------


def test_list_responses_carry_the_total_as_a_header(client: TestClient):
    register_workspace(client, email="count@example.com", workspace_name="Count")
    _add(client, "https://example.com/a.pdf https://example.com/b.pdf")

    response = client.get("/links")

    assert response.headers["X-Total-Count"] == "2"
    assert response.json()["total"] == 2, "the header must agree with the body"


def test_the_header_reflects_the_filter_not_the_workspace(client: TestClient):
    register_workspace(client, email="counth@example.com", workspace_name="CountH")
    _add(client, "https://example.com/a.pdf https://example.com/b.apk")

    response = client.get("/links", params={"category": "books_courses"})

    assert response.headers["X-Total-Count"] == "1"


# --- multi-value category --------------------------------------------------


def test_a_single_category_still_works(client: TestClient):
    register_workspace(client, email="cat1@example.com", workspace_name="Cat1")
    _add(client, "https://example.com/a.pdf https://example.com/b.apk")

    assert client.get("/links", params={"category": "books_courses"}).json()["total"] == 1


def test_several_categories_can_be_requested_at_once(client: TestClient):
    register_workspace(client, email="cat2@example.com", workspace_name="Cat2")
    _add(client, "https://example.com/a.pdf https://example.com/b.apk https://example.com/c.mp4")

    body = client.get("/links", params={"category": "books_courses,software_apps"}).json()

    assert body["total"] == 2
    assert {i["category"] for i in body["items"]} == {"books_courses", "software_apps"}


def test_whitespace_around_categories_is_tolerated(client: TestClient):
    register_workspace(client, email="cat3@example.com", workspace_name="Cat3")
    _add(client, "https://example.com/a.pdf https://example.com/b.apk")

    assert client.get("/links", params={"category": "books_courses , software_apps"}).json()["total"] == 2


def test_an_unknown_category_in_a_list_simply_matches_nothing(client: TestClient):
    """Filtering is not validation: an unknown value returns no rows rather
    than an error, the same as it always has for a single value."""
    register_workspace(client, email="cat4@example.com", workspace_name="Cat4")
    _add(client, "https://example.com/a.pdf")

    assert client.get("/links", params={"category": "books_courses,nonsense"}).json()["total"] == 1


# --- ETag on stats ---------------------------------------------------------


def test_stats_returns_an_etag(client: TestClient):
    register_workspace(client, email="etag1@example.com", workspace_name="ETag1")

    response = client.get("/links/stats")

    assert response.headers["ETag"].startswith('W/"')


def test_an_unchanged_workspace_gets_304(client: TestClient):
    register_workspace(client, email="etag2@example.com", workspace_name="ETag2")
    etag = client.get("/links/stats").headers["ETag"]

    response = client.get("/links/stats", headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""


def test_adding_a_link_changes_the_etag(client: TestClient):
    register_workspace(client, email="etag3@example.com", workspace_name="ETag3")
    before = client.get("/links/stats").headers["ETag"]

    _add(client, "https://example.com/a.pdf")

    assert client.get("/links/stats").headers["ETag"] != before


def test_recategorising_changes_the_etag_even_though_counts_do_not_move(client: TestClient):
    """The reason the tag is computed from the rendered values rather than
    a cheap proxy like a row count: this change moves no count at all."""
    register_workspace(client, email="etag4@example.com", workspace_name="ETag4")
    link_id = _add(client, "https://example.com/a.pdf")
    before_body = client.get("/links/stats").json()
    before = client.get("/links/stats").headers["ETag"]

    client.patch(f"/links/{link_id}", json={"category": "games"})
    after_body = client.get("/links/stats").json()

    assert before_body["total_links"] == after_body["total_links"], "the count really is unchanged"
    assert client.get("/links/stats").headers["ETag"] != before


def test_another_workspaces_etag_does_not_match(client: TestClient):
    """Two empty workspaces render different bodies, so a tag from one must
    never satisfy a conditional request for the other."""
    register_workspace(client, email="etag5@example.com", workspace_name="ETag5")
    _add(client, "https://example.com/a.pdf")
    foreign = client.get("/links/stats").headers["ETag"]
    client.post("/auth/logout")

    register_workspace(client, email="etag6@example.com", workspace_name="ETag6")
    response = client.get("/links/stats", headers={"If-None-Match": foreign})

    assert response.status_code == 200


# --- documented boolean handling ------------------------------------------


def test_boolean_parameters_accept_the_documented_spellings(client: TestClient):
    register_workspace(client, email="bool1@example.com", workspace_name="Bool1")

    for value in ("true", "True", "1", "yes", "on", "false", "0", "no", "off"):
        assert client.get("/links", params={"favorite": value}).status_code == 200, value


def test_a_nonsense_boolean_is_rejected_rather_than_guessed(client: TestClient):
    register_workspace(client, email="bool2@example.com", workspace_name="Bool2")

    assert client.get("/links", params={"favorite": "maybe"}).status_code == 422


# --- OpenAPI examples that cannot quietly go stale (idea 110) --------------


def _declared_models() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(obj, BaseModel) and obj is not BaseModel and obj.__module__ == schemas.__name__
    ]


def test_every_schema_declares_an_example():
    """Coverage, enforced rather than intended. A new schema added without
    an example fails here instead of shipping an OpenAPI entry that shows
    a client nothing but the field types."""
    without = [
        model.__name__
        for model in _declared_models()
        if not (model.model_config.get("json_schema_extra") or {}).get("examples")  # type: ignore[union-attr]
    ]

    assert without == [], f"schemas with no OpenAPI example: {without}"


def test_every_example_still_matches_its_own_schema():
    """The reason the examples are worth having. A hand-written example is
    documentation that drifts the first time a field is renamed; feeding it
    back through its own model turns that drift into a build failure."""
    models = _declared_models()
    assert len(models) > 25, "expected the whole schema module, not a subset"

    for model in models:
        for example in model.model_config["json_schema_extra"]["examples"]:  # type: ignore[index,call-overload]
            model.model_validate(example)


def test_the_examples_reach_the_published_openapi_document(client: TestClient):
    """Validating them in Python proves nothing if they never leave it."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]

    assert schema["LinkOut"]["examples"][0]["url"] == "https://peps.python.org/pep-0703/"
    # Nested schemas show the same values as the list that contains them,
    # because both read one constant.
    assert schema["SearchResponse"]["examples"][0]["items"][0] == schema["LinkOut"]["examples"][0]


# --- GET /auth/me/summary (idea 115) --------------------------------------


def test_the_summary_agrees_with_the_endpoints_it_summarises(client: TestClient):
    """The whole risk of an aggregate is that it becomes a second, slightly
    wrong source of truth. Every field is compared against the endpoint
    that owns it rather than against a hard-coded number."""
    register_workspace(client, email="sum1@example.com", workspace_name="Sum1")
    _add(client, "https://example.com/a.pdf")
    _add(client, "https://example.com/b.pdf")
    client.post("/channels", json={"tg_channel_id": "-100777", "username": "chan"})

    summary = client.get("/auth/me/summary").json()
    me = client.get("/auth/me").json()
    stats = client.get("/links/stats").json()
    sessions = client.get("/auth/sessions").json()
    security = client.get("/auth/security-activity").json()

    assert summary["user_id"] == me["id"]
    assert summary["email"] == me["email"]
    assert summary["workspace_id"] == me["workspace_id"]
    assert summary["workspace_name"] == me["workspace_name"]
    assert summary["total_links"] == stats["total_links"]
    assert summary["total_channels"] == stats["total_channels"]
    assert summary["active_sessions"] == len(sessions)
    assert summary["failed_logins_recent"] == security["failed_attempts"]


def test_the_summary_separates_working_accounts_from_disabled_ones(client: TestClient):
    """One total would read as healthy while collection had stopped."""
    register_workspace(client, email="sum2@example.com", workspace_name="Sum2")
    workspace_id = client.get("/auth/me").json()["workspace_id"]

    with SessionLocal() as db:
        db.add(TelegramAccount(workspace_id=workspace_id, label="live", session_string="x", is_active=True))
        db.add(
            TelegramAccount(
                workspace_id=workspace_id,
                label="revoked",
                session_string="y",
                is_active=False,
                disabled_reason="3 consecutive failures",
            )
        )
        db.commit()

    summary = client.get("/auth/me/summary").json()

    assert summary["active_accounts"] == 1
    assert summary["disabled_accounts"] == 1


def test_the_summary_counts_only_the_callers_own_workspace(client: TestClient):
    register_workspace(client, email="sum3@example.com", workspace_name="Sum3")
    _add(client, "https://example.com/theirs.pdf")
    _add(client, "https://example.com/theirs2.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="sum4@example.com", workspace_name="Sum4")
    summary = client.get("/auth/me/summary").json()

    assert summary["total_links"] == 0
    assert summary["workspace_name"] == "Sum4"


def test_the_summary_needs_a_session(client: TestClient):
    assert client.get("/auth/me/summary").status_code == 401


# --- /readyz diagnostics (idea 118) ---------------------------------------


def test_readiness_carries_no_diagnostics_unless_asked(client: TestClient):
    """The platform calls this on a schedule; the default path must stay a
    single SELECT and nothing else."""
    body = client.get("/readyz").json()

    assert body["status"] == "ready"
    assert "diagnostics" not in body


def test_a_rejected_password_carries_its_own_code(client: TestClient):
    """A client that wants to say "pick a stronger one" rather than "bad
    request" should not have to match on the prose to tell the difference."""
    response = client.post(
        "/auth/register",
        json={"email": "weak@example.com", "password": "password", "workspace_name": "Weak"},
    )

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.WEAK_PASSWORD
    assert isinstance(response.json()["detail"], str)
