"""What search and export do with input nobody sensible would send.

§12 and §25 ask for the adversarial and the merely realistic: an
unauthenticated request, another workspace's identifier, a malformed
filter, oversized pagination, an injection payload, an empty result. The
pass condition is not "no crash". It is that each one produces the *right*
kind of answer — 401 for who you are, 422 for what you sent, 200 with zero
rows for a question that simply has no matches — because collapsing those
three into one response is how a filter that silently stopped working goes
unnoticed.

Ordering matters here and is asserted: authentication is resolved before
validation, so a malformed request from an anonymous caller answers 401
rather than 422. The other way round tells someone with no credentials
which parameters exist and what shapes they take.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.classifier import CATEGORIES
from tests.conftest import register_workspace

SEARCH_SURFACE = ("/links", "/links/export.csv", "/links/export.json", "/links/export.md")


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    register_workspace(client, email="hostile@example.com", workspace_name="Hostile Co")
    client.post("/links", json={"text": "https://example.com/present.pdf"})
    return client


# --- who you are, before what you sent -------------------------------------


@pytest.mark.parametrize("path", SEARCH_SURFACE)
def test_an_anonymous_caller_is_refused_before_anything_is_parsed(client: TestClient, path: str):
    """401 for a malformed request too, not 422.

    A 422 here would answer a question the caller has not earned: it
    confirms the parameter exists and describes the shape it wants.
    """
    assert client.get(path).status_code == 401
    assert client.get(path, params={"page_size": 100000, "since": "not-a-date"}).status_code == 401


def test_an_anonymous_caller_cannot_reach_the_import(client: TestClient):
    assert client.post("/channels/import", json={"text": "@x"}).status_code == 401


# --- what you sent ---------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "why"),
    [
        ({"page_size": 100000}, "a page size past the cap"),
        ({"page_size": 0}, "a page size of zero"),
        ({"page": -1}, "a negative page"),
        ({"page": 0}, "page zero"),
        ({"since": "not-a-date"}, "an unparseable date"),
        ({"until": "2026-13-45"}, "an impossible date"),
        ({"q": "x" * 400}, "a search term past its length cap"),
        ({"favorite": "maybe"}, "a boolean that is not one"),
        ({"channel_id": "abc"}, "a non-numeric id"),
        ({"sort": "no_such_sort"}, "an unknown sort"),
        ({"platform": "x" * 500}, "a platform past its length cap"),
        ({"category": "x" * 500}, "a category past its length cap"),
        ({"language": "x" * 50}, "a language past its length cap"),
        ({"domain": "x" * 500}, "a domain past its length cap"),
    ],
)
def test_a_malformed_filter_is_refused_rather_than_ignored(signed_in: TestClient, params: dict, why: str):
    """Refused, not quietly dropped.

    A filter the server ignores returns *more* rows than were asked for,
    with a 200 — the same silent widening AC-SR06 is about, arriving
    through validation instead of through the query builder.
    """
    assert signed_in.get("/links", params=params).status_code == 422, why


@pytest.mark.parametrize("params", [{"sort": "no_such_sort"}, {"since": "nope"}, {"category": "x" * 500}])
def test_the_exports_refuse_what_the_search_refuses(signed_in: TestClient, params: dict):
    """One filter contract, so one set of rejections.

    They share a dependency now; before that each export validated
    whatever it happened to declare, and an export could accept a value
    search rejected.
    """
    for path in SEARCH_SURFACE:
        assert signed_in.get(path, params=params).status_code == 422, path


def test_page_size_is_a_search_parameter_and_the_exports_ignore_it(signed_in: TestClient):
    """Deliberately *not* part of the shared filter set.

    ``page`` and ``page_size`` slice a result set; the filters decide what
    is in it. An export streams the whole scope by definition, so it takes
    the filters and not the slice — and an oversized ``page_size`` sent to
    an export is an unknown parameter rather than an invalid one, which is
    why it answers 200 where search answers 422.

    Pinned because the asymmetry looks like an oversight and is not: an
    export that honoured ``page_size`` would silently truncate the file.
    """
    signed_in.post("/links", json={"text": "https://example.com/second.pdf"})

    assert signed_in.get("/links", params={"page_size": 100000}).status_code == 422
    assert signed_in.get("/links/export.csv", params={"page_size": 1}).status_code == 200
    assert len(signed_in.get("/links/export.json", params={"page_size": 1}).json()) == 2


def test_every_real_category_still_fits_inside_the_length_cap(signed_in: TestClient):
    """The cap must not refuse the widest legitimate request.

    Asking for every category at once is an ordinary thing to do, and a
    bound tight enough to break it would be a bug wearing a limit's
    clothes. Measured against the real list rather than a number copied
    into the test, so adding a category fails here rather than in
    production.
    """
    everything = ",".join(CATEGORIES)

    assert len(everything) <= 200, (
        f"the category list is now {len(everything)} characters and no longer fits "
        "the filter's 200-character cap — raise the cap in link_filters"
    )
    assert signed_in.get("/links", params={"category": everything}).status_code == 200


# --- payloads --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE links;--",
        "' OR 1=1 --",
        "%' UNION SELECT session_string FROM telegram_accounts --",
        "\x00null byte",
        "\\",
        "((((",
        "*:*",
        "قناة' OR '1'='1",
    ],
)
def test_an_injection_payload_is_treated_as_text(signed_in: TestClient, payload: str):
    """The term is data, never syntax — on both search backends.

    Postgres takes ``plainto_tsquery``, which treats its whole input as
    literal terms rather than a query language; SQLite takes a bound
    ILIKE. Neither can be escaped into, and the data is still here
    afterwards — which is the assertion that would notice if one day it
    could be.
    """
    response = signed_in.get("/links", params={"q": payload})

    assert response.status_code == 200
    assert signed_in.get("/links").json()["total"] == 1, "the payload changed the data"


def test_a_very_long_url_and_a_very_long_term_do_not_break_search(signed_in: TestClient):
    """§25's "very long URL" and "long context", as one realistic pair."""
    signed_in.post("/links", json={"text": "https://example.com/" + "a" * 1500 + ".pdf"})

    assert signed_in.get("/links", params={"q": "a" * 299}).status_code == 200
    assert signed_in.get("/links").json()["total"] == 2


def test_malformed_unicode_in_a_search_term_is_answered_not_crashed(signed_in: TestClient):
    # No unpaired surrogate here: it cannot be encoded into an HTTP
    # request at all, so a test using one measures the client, not the
    # server. These are strings a real message can actually contain.
    for term in ("🙂" * 50, "و" + "ـ" * 100, "‮", "\N{ZERO WIDTH JOINER}" * 40, "é" * 200):
        assert signed_in.get("/links", params={"q": term}).status_code == 200


# --- pagination boundaries -------------------------------------------------


def test_a_page_past_the_end_is_empty_and_not_an_error(signed_in: TestClient):
    """AC-SR05, at the boundary that produces it most often."""
    body = signed_in.get("/links", params={"page": 10_000, "page_size": 100}).json()

    assert body["total"] == 1
    assert body["items"] == []


def test_the_total_header_and_the_body_agree(signed_in: TestClient):
    response = signed_in.get("/links", params={"page": 10_000})

    assert response.headers["X-Total-Count"] == str(response.json()["total"])
