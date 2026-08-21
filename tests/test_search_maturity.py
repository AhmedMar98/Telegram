"""Search that can narrow, name itself, and leave the building.

Three things a collection needs once it stops being small: a way to say
what you *don't* want, a way to come back to a query you use weekly, and
a way to hand a curated result to a person rather than to a program.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.search import parse_query
from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> None:
    assert client.post("/links", json={"text": text}).status_code == 201


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "include", "exclude"),
    [
        ("دورة بايثون", "دورة بايثون", []),
        ("دورة بايثون -مدفوع", "دورة بايثون", ["مدفوع"]),
        ("-spam", "", ["spam"]),
        ("a -b -c", "a", ["b", "c"]),
        ("", "", []),
    ],
)
def test_parse_query_splits_include_from_exclude(raw, include, exclude):
    parsed = parse_query(raw)
    assert parsed.include == include
    assert parsed.exclude == exclude


def test_a_hyphen_inside_a_word_is_not_an_exclusion():
    """ "python-book" must still search for the hyphenated term — treating
    the inner hyphen as negation would silently exclude "book"."""
    assert parse_query("python-book").exclude == []
    assert parse_query("python-book").include == "python-book"


def test_a_bare_hyphen_is_not_an_exclusion():
    assert parse_query("- x").exclude == []


# --- exclusion at the API boundary ----------------------------------------


def test_a_negated_term_removes_matching_links(client: TestClient):
    register_workspace(client, email="exc@example.com", workspace_name="Exc")
    _add(client, "دورة بايثون مجانية https://example.com/free-python.pdf")
    _add(client, "دورة بايثون مدفوعة https://example.com/paid-python.pdf")

    results = client.get("/links", params={"q": "بايثون -مدفوعة"}).json()

    assert [i["url"] for i in results["items"]] == ["https://example.com/free-python.pdf"]


def test_exclusion_alone_returns_everything_else(client: TestClient):
    """A query that is only negative is meaningful: "all of it except this"."""
    register_workspace(client, email="exconly@example.com", workspace_name="ExcOnly")
    _add(client, "ملف عادي https://example.com/keep.pdf")
    _add(client, "إعلان مزعج https://example.com/spam.pdf")

    results = client.get("/links", params={"q": "-مزعج"}).json()

    assert [i["url"] for i in results["items"]] == ["https://example.com/keep.pdf"]


def test_exclusion_matches_the_url_as_well_as_the_text(client: TestClient):
    register_workspace(client, email="excurl@example.com", workspace_name="ExcUrl")
    _add(client, "https://good.example/a.pdf")
    _add(client, "https://tracker.example/b.pdf")

    results = client.get("/links", params={"q": "-tracker"}).json()

    assert [i["url"] for i in results["items"]] == ["https://good.example/a.pdf"]


def test_excluding_every_result_is_empty_not_an_error(client: TestClient):
    register_workspace(client, email="excall@example.com", workspace_name="ExcAll")
    _add(client, "كتاب https://example.com/a.pdf")

    response = client.get("/links", params={"q": "-كتاب"})
    assert response.status_code == 200
    assert response.json()["total"] == 0


# --- domain filter and frequency sort -------------------------------------


def test_search_can_be_filtered_by_domain(client: TestClient):
    register_workspace(client, email="dom@example.com", workspace_name="Dom")
    _add(client, "https://alpha.example/a.pdf https://beta.example/b.pdf")

    results = client.get("/links", params={"domain": "alpha.example"}).json()
    assert [i["url"] for i in results["items"]] == ["https://alpha.example/a.pdf"]


def test_the_domain_filter_is_case_insensitive(client: TestClient):
    """Stored domains are lowercased at ingest; a filter typed in capitals
    must still match rather than silently returning nothing."""
    register_workspace(client, email="domcase@example.com", workspace_name="DomCase")
    _add(client, "https://alpha.example/a.pdf")

    assert client.get("/links", params={"domain": "ALPHA.example"}).json()["total"] == 1


def test_the_domain_filter_combines_with_the_category_filter(client: TestClient):
    """This pairing is what the "similar links" button sends."""
    register_workspace(client, email="domcat@example.com", workspace_name="DomCat")
    _add(client, "https://alpha.example/a.pdf https://alpha.example/b.apk")

    results = client.get("/links", params={"domain": "alpha.example", "category": "books_courses"}).json()
    assert [i["url"] for i in results["items"]] == ["https://alpha.example/a.pdf"]


def test_sorting_by_domain_frequency_puts_the_busiest_source_first(client: TestClient):
    register_workspace(client, email="freq@example.com", workspace_name="Freq")
    _add(client, "https://rare.example/only.pdf")
    _add(client, "https://common.example/a.pdf https://common.example/b.pdf https://common.example/c.pdf")

    items = client.get("/links", params={"sort": "domain_frequency"}).json()["items"]

    assert [i["domain"] for i in items] == ["common.example"] * 3 + ["rare.example"]


def test_an_unknown_sort_is_still_rejected(client: TestClient):
    register_workspace(client, email="badsort@example.com", workspace_name="BadSort")
    assert client.get("/links", params={"sort": "whatever"}).status_code == 422


# --- random discovery ------------------------------------------------------


def test_random_returns_at_most_the_requested_count(client: TestClient):
    register_workspace(client, email="rnd@example.com", workspace_name="Rnd")
    _add(client, " ".join(f"https://example.com/{i}.pdf" for i in range(10)))

    assert len(client.get("/links/random", params={"count": 3}).json()) == 3


def test_random_on_an_empty_workspace_is_an_empty_list(client: TestClient):
    register_workspace(client, email="rndempty@example.com", workspace_name="RndEmpty")
    assert client.get("/links/random").json() == []


def test_random_never_crosses_into_another_workspace(client: TestClient):
    register_workspace(client, email="rnd1@example.com", workspace_name="Rnd1")
    _add(client, "https://example.com/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="rnd2@example.com", workspace_name="Rnd2")
    assert client.get("/links/random", params={"count": 20}).json() == []


def test_random_excludes_archived_links(client: TestClient):
    register_workspace(client, email="rndarch@example.com", workspace_name="RndArch")
    _add(client, "https://example.com/a.pdf")
    link_id = client.get("/links").json()["items"][0]["id"]
    client.post(f"/links/{link_id}/archive")

    assert client.get("/links/random").json() == []


def test_random_requires_authentication(client: TestClient):
    assert client.get("/links/random").status_code == 401


# --- saved searches --------------------------------------------------------


def test_a_saved_search_round_trips(client: TestClient):
    register_workspace(client, email="sav@example.com", workspace_name="Sav")

    created = client.post("/links/saved", json={"name": "كتب عربية", "filters": {"q": "كتاب", "language": "ar"}})
    assert created.status_code == 201

    listed = client.get("/links/saved").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "كتب عربية"
    assert listed[0]["filters"] == {"q": "كتاب", "language": "ar"}


def test_saving_the_same_name_replaces_rather_than_duplicates(client: TestClient):
    register_workspace(client, email="savdup@example.com", workspace_name="SavDup")
    client.post("/links/saved", json={"name": "مهم", "filters": {"q": "أ"}})

    client.post("/links/saved", json={"name": "مهم", "filters": {"q": "ب"}})

    listed = client.get("/links/saved").json()
    assert len(listed) == 1
    assert listed[0]["filters"] == {"q": "ب"}


def test_an_unknown_filter_key_is_refused(client: TestClient):
    """A saved search is replayed into a later request, so it must not be
    able to carry a parameter the caller never chose."""
    register_workspace(client, email="savbad@example.com", workspace_name="SavBad")

    response = client.post("/links/saved", json={"name": "x", "filters": {"page_size": "100000"}})

    assert response.status_code == 422
    assert "page_size" in response.json()["detail"]


def test_a_saved_search_can_be_deleted(client: TestClient):
    register_workspace(client, email="savdel@example.com", workspace_name="SavDel")
    saved_id = client.post("/links/saved", json={"name": "x", "filters": {"q": "y"}}).json()["id"]

    assert client.delete(f"/links/saved/{saved_id}").status_code == 204
    assert client.get("/links/saved").json() == []


def test_saved_searches_are_workspace_scoped(client: TestClient):
    register_workspace(client, email="sav1@example.com", workspace_name="Sav1")
    client.post("/links/saved", json={"name": "theirs", "filters": {"q": "secret"}})
    client.post("/auth/logout")

    register_workspace(client, email="sav2@example.com", workspace_name="Sav2")
    assert client.get("/links/saved").json() == []


def test_the_same_name_may_be_used_in_two_workspaces(client: TestClient):
    """Uniqueness is per workspace, not global — otherwise one tenant could
    discover which names another has taken."""
    register_workspace(client, email="savn1@example.com", workspace_name="SavN1")
    assert client.post("/links/saved", json={"name": "المفضّلة", "filters": {}}).status_code == 201
    client.post("/auth/logout")

    register_workspace(client, email="savn2@example.com", workspace_name="SavN2")
    assert client.post("/links/saved", json={"name": "المفضّلة", "filters": {}}).status_code == 201


def test_saved_searches_are_capped(client: TestClient, monkeypatch):
    from app.routers import links as links_module

    monkeypatch.setattr(links_module, "MAX_SAVED_SEARCHES", 2)
    register_workspace(client, email="savcap@example.com", workspace_name="SavCap")

    for i in range(2):
        assert client.post("/links/saved", json={"name": f"s{i}", "filters": {}}).status_code == 201

    assert client.post("/links/saved", json={"name": "s3", "filters": {}}).status_code == 422
    # Overwriting an existing one is not blocked by the cap.
    assert client.post("/links/saved", json={"name": "s0", "filters": {"q": "z"}}).status_code == 201


def test_saved_searches_require_authentication(client: TestClient):
    assert client.get("/links/saved").status_code == 401
    assert client.post("/links/saved", json={"name": "x", "filters": {}}).status_code == 401


# --- exports honour the search term ---------------------------------------


def test_csv_export_honours_the_search_term(client: TestClient):
    """Exporting "these results" used to silently export the whole
    workspace, which is the opposite of what the button says."""
    register_workspace(client, email="expq@example.com", workspace_name="ExpQ")
    _add(client, "كتاب مفيد https://example.com/book.pdf")
    _add(client, "فيلم قديم https://example.com/movie.mp4")

    rows = list(csv.reader(io.StringIO(client.get("/links/export.csv", params={"q": "كتاب"}).text)))[1:]

    assert [r[0] for r in rows] == ["https://example.com/book.pdf"]


def test_json_export_honours_the_search_term(client: TestClient):
    register_workspace(client, email="expqj@example.com", workspace_name="ExpQJ")
    _add(client, "كتاب مفيد https://example.com/book.pdf")
    _add(client, "فيلم قديم https://example.com/movie.mp4")

    rows = client.get("/links/export.json", params={"q": "فيلم"}).json()

    assert [r["url"] for r in rows] == ["https://example.com/movie.mp4"]


# --- markdown export -------------------------------------------------------


def test_markdown_export_groups_by_category(client: TestClient):
    register_workspace(client, email="md@example.com", workspace_name="MD")
    _add(client, "كتاب مفيد https://example.com/book.pdf")
    _add(client, "تطبيق جديد https://example.com/app.apk")

    response = client.get("/links/export.md")

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert "## books_courses" in body
    assert "## software_apps" in body
    assert "](https://example.com/book.pdf)" in body


def test_markdown_export_honours_the_search_term(client: TestClient):
    register_workspace(client, email="mdq@example.com", workspace_name="MDQ")
    _add(client, "كتاب مفيد https://example.com/book.pdf")
    _add(client, "فيلم قديم https://example.com/movie.mp4")

    body = client.get("/links/export.md", params={"q": "كتاب"}).text

    assert "book.pdf" in body
    assert "movie.mp4" not in body


def test_markdown_export_escapes_link_syntax_in_the_label(client: TestClient):
    """The label comes from a Telegram message. Unescaped brackets let it
    break out of the [label](url) it is placed inside.

    The decoy text deliberately contains no second URL: a real URL in the
    message would be extracted and stored as its own link, which would make
    this test pass for the wrong reason.
    """
    register_workspace(client, email="mdesc@example.com", workspace_name="MDEsc")
    _add(client, "عنوان [مزيف](خدعة) هنا https://example.com/real.pdf")

    body = client.get("/links/export.md").text

    assert "[مزيف](خدعة)" not in body, "raw markdown link syntax survived into the label"
    assert r"\[مزيف\]\(خدعة\)" in body
    assert "](https://example.com/real.pdf)" in body


def test_markdown_export_marks_dead_links(client: TestClient):
    from app.database import SessionLocal
    from app.models import Link

    register_workspace(client, email="mddead@example.com", workspace_name="MDDead")
    _add(client, "كتاب https://example.com/gone.pdf")

    db = SessionLocal()
    try:
        db.query(Link).update({"is_alive": False})
        db.commit()
    finally:
        db.close()

    assert "رابط ميت" in client.get("/links/export.md").text


def test_markdown_export_of_an_empty_workspace_is_just_front_matter_and_a_heading(client: TestClient):
    """Idea 166 added the YAML block, so this is no longer only the
    heading — but the point it was written to pin still holds: an empty
    workspace produces no link lines and no empty category sections."""
    register_workspace(client, email="mdempty@example.com", workspace_name="MDEmpty")

    body = client.get("/links/export.md").text

    assert body.startswith("---\n")
    assert body.rstrip().endswith("# روابط")
    assert "- [" not in body
    assert "\n## " not in body


def test_markdown_export_requires_authentication(client: TestClient):
    assert client.get("/links/export.md").status_code == 401
