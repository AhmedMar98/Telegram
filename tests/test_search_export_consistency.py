"""What a search shows is what its export contains.

AC-SR06: "التصدير يطابق نطاق البحث/الفلاتر المعروضة ولا يصدّر بيانات خارج
النطاق المقصود." Export matches the displayed search scope and does not
export data outside the intended scope.

It did not. Search accepted ten filters; the three export endpoints
accepted four of them and forced ``include_archived=True`` on top. So a
caller who searched for their favourites and exported the result got the
whole workspace — every category, every domain, every archived link they
had deliberately hidden — with a 200 and no indication the filter had been
dropped. The failure is silent and points the wrong way: an export that
ignores a filter returns *more* than was asked for, which nothing in the
response can reveal and no user would think to check.

The fix is structural rather than four endpoints being edited to agree:
one dependency (``app.routers.links.link_filters``) declares the filter
set, and search and all three exports consume the same
``LinkFilters``. These tests hold that in place from the outside, by
comparing the sets of links each endpoint actually returns rather than
their counts — two different sets of the same size compare equal on a
count and are the exact bug being ruled out.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.models import Link
from tests.conftest import register_workspace


def _seed(workspace_id: int, channel_id: int, index: int, **overrides) -> str:
    """One link, distinct on every axis a filter can address."""
    url = overrides.pop("url", f"https://example.com/{index}.pdf")
    defaults = {
        "domain": "example.com",
        "platform": "web",
        "category": "other",
        "language": "en",
        "is_favorite": False,
        "is_archived": False,
        "is_alive": None,
        "raw_text": "plain text",
        "confidence": 0.5,
    }
    db = SessionLocal()
    try:
        db.add(
            Link(
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_id=index,
                url=url,
                url_hash=hash_url(url),
                classified_by="rules-v2",
                **{**defaults, **overrides},
            )
        )
        db.commit()
    finally:
        db.close()
    return url


@pytest.fixture
def populated(client: TestClient) -> tuple[TestClient, int]:
    """A workspace holding one link per filterable shape.

    Every link differs from every other on at least one axis, so a filter
    that is silently ignored returns a visibly different set rather than
    the same rows by luck.
    """
    register_workspace(client, email="scope@example.com", workspace_name="Scope Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    first = client.post("/channels", json={"tg_channel_id": "11", "username": "one"}).json()
    second = client.post("/channels", json={"tg_channel_id": "22", "username": "two"}).json()

    _seed(workspace_id, first["id"], 1)
    _seed(workspace_id, first["id"], 2, is_favorite=True)
    _seed(workspace_id, first["id"], 3, category="movies_series")
    _seed(workspace_id, first["id"], 4, platform="telegram", url="https://t.me/somewhere", domain="t.me")
    _seed(workspace_id, first["id"], 5, language="ar", raw_text="نص عربي")
    _seed(workspace_id, first["id"], 6, is_alive=True)
    _seed(workspace_id, first["id"], 7, is_archived=True)
    _seed(workspace_id, second["id"], 8)
    return client, second["id"]


def _searched(client: TestClient, params: dict) -> set[str]:
    """Every URL the search returns, across all its pages."""
    urls: set[str] = set()
    page = 1
    while True:
        body = client.get("/links", params={**params, "page": page, "page_size": 3}).json()
        urls.update(item["url"] for item in body["items"])
        if page * 3 >= body["total"]:
            return urls
        page += 1


def _exported_json(client: TestClient, params: dict) -> set[str]:
    return {row["url"] for row in client.get("/links/export.json", params=params).json()}


def _exported_csv(client: TestClient, params: dict) -> set[str]:
    text = client.get("/links/export.csv", params=params).text
    return {row["url"] for row in csv.DictReader(io.StringIO(text))}


def _exported_markdown(client: TestClient, params: dict) -> set[str]:
    body = client.get("/links/export.md", params=params).text
    return {line.split("](", 1)[1].rstrip(")") for line in body.splitlines() if line.startswith("- [")}


# Each entry is a filter the search offers. Six of the eight were accepted
# by search and ignored by every export before this was one shared
# dependency; ``since``/``until`` were the mirror image, accepted by the
# exports and unavailable on search.
FILTERS: list[dict] = [
    {},
    {"favorite": True},
    {"category": "movies_series"},
    {"platform": "telegram"},
    {"language": "ar"},
    {"alive": True},
    {"domain": "t.me"},
    {"include_archived": True},
    {"q": "عربي"},
    {"since": "2099-01-01"},
    {"until": "2099-01-01"},
    {"favorite": True, "category": "other"},
    {"platform": "web", "language": "en", "include_archived": True},
]


@pytest.mark.parametrize(
    "params", FILTERS, ids=lambda p: ",".join(f"{k}={v}" for k, v in p.items()) or "no-filter"
)
def test_every_export_format_returns_exactly_what_the_search_returns(populated, params):
    """The same filter, four endpoints, one set of links.

    Compared as sets of URLs, not as counts: an export that swapped one
    link for another would keep the count and fail here, which is the
    whole point.
    """
    client, _ = populated
    expected = _searched(client, params)

    assert _exported_json(client, params) == expected, "export.json disagrees with the search"
    assert _exported_csv(client, params) == expected, "export.csv disagrees with the search"
    assert _exported_markdown(client, params) == expected, "export.md disagrees with the search"


def test_the_channel_filter_holds_across_search_and_export(populated):
    """Separate from the table above because the id is only known here."""
    client, second_channel = populated
    params = {"channel_id": second_channel}

    expected = _searched(client, params)

    assert len(expected) == 1, "fixture drift: the second channel should hold exactly one link"
    assert _exported_json(client, params) == expected
    assert _exported_csv(client, params) == expected
    assert _exported_markdown(client, params) == expected


def test_csv_and_json_stream_the_rows_in_the_search_order(populated):
    """Not just the same rows — the same sequence.

    Two exports of one search that disagree on order are two different
    documents, and a caller diffing yesterday's export against today's
    would see every row as changed.
    """
    client, _ = populated
    params = {"sort": "domain", "include_archived": True}

    searched = [item["url"] for item in client.get("/links", params={**params, "page_size": 100}).json()["items"]]
    exported = [row["url"] for row in client.get("/links/export.json", params=params).json()]
    from_csv = [
        row["url"] for row in csv.DictReader(io.StringIO(client.get("/links/export.csv", params=params).text))
    ]

    assert exported == searched
    assert from_csv == searched


def test_an_export_cannot_reach_another_workspace(client: TestClient):
    """Scope is the workspace first and the filter second.

    A filter naming another workspace's channel matches nothing rather
    than leaking whether that channel exists — the same property the
    search relies on, asserted against the export because it is a second
    query and could have been written without it.
    """
    register_workspace(client, email="victim@example.com", workspace_name="Victim")
    victim_workspace = client.get("/auth/me").json()["workspace_id"]
    victim_channel = client.post("/channels", json={"tg_channel_id": "99", "username": "v"}).json()
    _seed(victim_workspace, victim_channel["id"], 1, url="https://secret.example/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="attacker@example.com", workspace_name="Attacker")

    for params in (
        {},
        {"channel_id": victim_channel["id"]},
        {"include_archived": True},
        {"domain": "secret.example"},
    ):
        assert _exported_json(client, params) == set()
        assert _exported_csv(client, params) == set()


def test_an_unknown_sort_is_refused_by_the_exports_too(populated):
    """The exports validate ``sort`` because they share the dependency.

    Worth pinning: the check used to live in the search endpoint's body,
    so an export was free to accept a sort name the search rejected.
    """
    client, _ = populated

    for path in ("/links", "/links/export.csv", "/links/export.json", "/links/export.md"):
        assert client.get(path, params={"sort": "no_such_sort"}).status_code == 422, path


def test_an_empty_result_is_a_result_and_not_an_error(populated):
    """AC-SR05: "لا توجد نتائج = نتيجة صحيحة، وليست خطأ تقنيًا بحد ذاتها"."""
    client, _ = populated
    params = {"category": "no_such_category"}

    search = client.get("/links", params=params)
    assert search.status_code == 200
    assert search.json()["total"] == 0

    for path in ("/links/export.csv", "/links/export.json", "/links/export.md"):
        assert client.get(path, params=params).status_code == 200, path

    assert _exported_json(client, params) == set()
    assert _exported_csv(client, params) == set()
