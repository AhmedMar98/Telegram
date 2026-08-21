"""Import and export interoperability with the tools people already use.

Phase 8b. The recurring risk in this area is a format that works on the
one file the author happened to try: real exports are years old, written
by four different browsers, occasionally truncated, and full of entries
that are not links at all. So most of what follows is hostile input.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.bookmarks import parse, parse_bookmark_csv, parse_netscape_html
from app.database import SessionLocal
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.models import Channel, Link, Workspace
from app.vitality import status_category
from scripts.import_bookmarks import run as import_bookmarks
from tests.conftest import register_workspace

CHROME_EXPORT = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1600000000">Reading</H3>
    <DL><p>
        <DT><A HREF="https://peps.python.org/pep-0703/" ADD_DATE="1750000000" TAGS="python,perf">PEP 703</A>
        <DT><A HREF="http://example.com/a">Plain &amp; escaped</A>
    </DL><p>
</DL><p>
"""


# --- Netscape bookmark HTML (idea 168) -------------------------------------


def test_a_browser_export_yields_its_links_with_titles_and_dates():
    result = parse_netscape_html(CHROME_EXPORT)

    assert [b.url for b in result.bookmarks] == [
        "https://peps.python.org/pep-0703/",
        "http://example.com/a",
    ]
    first = result.bookmarks[0]
    assert first.title == "PEP 703"
    assert first.tags == ["python", "perf"]
    assert first.added_at is not None
    # HTML entities are decoded, not stored raw.
    assert result.bookmarks[1].title == "Plain & escaped"


def test_bookmarklets_and_browser_internal_entries_never_become_links():
    """Every real bookmark bar has these. A javascript: row in the links
    table is a row nothing else in the system can explain — /links/{id}/open
    re-checks the scheme precisely because such a row means something wrote
    it by another path. This is that path, and it stops here."""
    hostile = """<DL>
      <DT><A HREF="javascript:(function(){alert(document.cookie)})()">Bookmarklet</A>
      <DT><A HREF="chrome://bookmarks/">Manager</A>
      <DT><A HREF="place:sort=8&maxResults=10">Firefox smart folder</A>
      <DT><A HREF="file:///etc/passwd">Local file</A>
      <DT><A HREF="data:text/html;base64,PHNjcmlwdD4=">Data URI</A>
      <DT><A HREF="https://example.com/real">Real one</A>
    </DL>"""

    result = parse_netscape_html(hostile)

    assert [b.url for b in result.bookmarks] == ["https://example.com/real"]
    assert result.skipped_unsupported_scheme == 5


def test_a_truncated_file_keeps_the_entries_completed_before_the_break():
    """Exports get cut short by a full disk or an interrupted download.
    Losing the last bookmark is acceptable; losing all of them is not.

    Only the *completed* entry is asserted. Whether a half-written final
    tag is recovered depends on the interpreter: CPython 3.11.15 emits a
    start tag for unterminated markup at EOF and 3.11.16 does not, which
    is a hardening change rather than a bug in either. Pinning the
    recovered one made this pass locally and fail in CI — a test asserting
    more than the language guarantees."""
    result = parse_netscape_html(
        '<DL><DT><A HREF="https://example.com/one">One</A>\n<DT><A HREF="https://example.com/two">Tw'
    )

    urls = [b.url for b in result.bookmarks]

    assert "https://example.com/one" in urls
    assert set(urls) <= {"https://example.com/one", "https://example.com/two"}


@pytest.mark.parametrize(
    "junk",
    ["", "   ", "not html at all", "<DL><DT><A HREF=", "\x00\x01\x02", "<A>no href</A>", "<a href=''></a>"],
)
def test_no_input_makes_the_html_parser_raise(junk: str):
    result = parse_netscape_html(junk)

    assert isinstance(result.bookmarks, list)


def test_an_entry_with_no_href_is_counted_as_malformed_not_dropped_silently():
    result = parse_netscape_html('<DL><DT><A>No target</A><DT><A HREF="https://example.com/x">Fine</A></DL>')

    assert len(result.bookmarks) == 1
    assert result.skipped_malformed == 1


# --- Pocket / Instapaper CSV (idea 169) ------------------------------------


def test_a_pocket_export_is_read():
    csv_text = (
        'url,title,time_added,tags,status\nhttps://example.com/p,Pocket item,1750000000,"read,later",unread\n'
    )

    result = parse_bookmark_csv(csv_text)

    assert result.bookmarks[0].url == "https://example.com/p"
    assert result.bookmarks[0].title == "Pocket item"
    assert result.bookmarks[0].tags == ["read", "later"]
    assert result.bookmarks[0].added_at is not None


def test_an_instapaper_export_is_read_by_the_same_parser():
    """The two differ only in header spelling. Detecting that beats a
    format argument the caller has to get right."""
    csv_text = "URL,Title,Selection,Folder,Timestamp\nhttps://example.com/i,Instapaper item,,Archive,1750000000\n"

    result = parse_bookmark_csv(csv_text)

    assert result.bookmarks[0].url == "https://example.com/i"
    assert result.bookmarks[0].title == "Instapaper item"
    assert result.bookmarks[0].tags == ["Archive"]


def test_rows_without_a_url_are_skipped_rather_than_fatal():
    csv_text = "url,title\n,No URL\nhttps://example.com/ok,Fine\njavascript:void(0),Bad scheme\n"

    result = parse_bookmark_csv(csv_text)

    assert [b.url for b in result.bookmarks] == ["https://example.com/ok"]
    assert result.skipped_malformed == 1
    assert result.skipped_unsupported_scheme == 1


def test_an_unparseable_timestamp_is_recorded_as_unknown_not_invented():
    """A fabricated date on a real row is worse than no date."""
    csv_text = "url,time_added\nhttps://example.com/a,not-a-number\nhttps://example.com/b,0\nhttps://example.com/c,99999999999999\n"

    result = parse_bookmark_csv(csv_text)

    assert [b.added_at for b in result.bookmarks] == [None, None, None]


def test_format_is_detected_from_content_not_extension():
    """A Pocket export saved as .txt is still a CSV; an HTML file named
    .csv is still HTML."""
    assert parse(CHROME_EXPORT, filename="whatever.csv").bookmarks
    assert parse("url,title\nhttps://example.com/z,Z\n", filename="pocket.txt").bookmarks


def test_the_classifier_sees_the_title_not_just_the_url():
    """Title and tags are the only description these formats carry, and a
    bare URL classifies markedly worse than one with words beside it."""
    result = parse_netscape_html(CHROME_EXPORT)

    text = result.bookmarks[0].as_text()

    assert "https://peps.python.org/pep-0703/" in text
    assert "PEP 703" in text
    assert "python" in text


# --- YAML front matter on the Markdown export (idea 166) -------------------


def _add(client: TestClient, text: str) -> None:
    assert client.post("/links", json={"text": text}).status_code == 201


def test_the_markdown_export_opens_with_yaml_front_matter(client: TestClient):
    register_workspace(client, email="fm1@example.com", workspace_name="FM1")
    _add(client, "كتاب https://example.com/a.pdf")

    body = client.get("/links/export.md").text

    assert body.startswith("---\n")
    header, _, rest = body.partition("\n---\n")
    assert "exported_at:" in header
    assert "source:" in header
    assert "# روابط" in rest


def test_a_search_term_with_quotes_cannot_break_the_front_matter(client: TestClient):
    """An unescaped quote ends the YAML scalar early and turns the block
    into a parse error for every reader that actually parses it."""
    register_workspace(client, email="fm2@example.com", workspace_name="FM2")
    _add(client, "https://example.com/a.pdf")

    body = client.get("/links/export.md", params={"q": 'he said "hi"\nand more'}).text
    header = body.split("\n---\n")[0]

    assert '"hi"' not in header
    # The query line stays a single line, so the block still closes.
    assert len([ln for ln in header.splitlines() if ln.startswith("query:")]) == 1


# --- export date window (idea 179) -----------------------------------------


def test_an_export_can_be_limited_to_a_date_window(client: TestClient):
    register_workspace(client, email="dw1@example.com", workspace_name="DW1")
    _add(client, "https://example.com/old.pdf")
    _add(client, "https://example.com/new.pdf")

    with SessionLocal() as db:
        old = db.query(Link).filter(Link.url == "https://example.com/old.pdf").one()
        old.created_at = old.created_at - timedelta(days=30)
        db.commit()

    today = date.today()
    recent = client.get("/links/export.csv", params={"since": (today - timedelta(days=2)).isoformat()}).text

    assert "new.pdf" in recent
    assert "old.pdf" not in recent


def test_the_window_includes_links_collected_during_the_final_day(client: TestClient):
    """`created_at <= until` compared against a bare date would exclude
    everything collected during that day, which reads as an off-by-one."""
    register_workspace(client, email="dw2@example.com", workspace_name="DW2")
    _add(client, "https://example.com/today.pdf")

    body = client.get("/links/export.csv", params={"until": date.today().isoformat()}).text

    assert "today.pdf" in body


def test_a_nonsense_date_is_rejected_rather_than_ignored(client: TestClient):
    register_workspace(client, email="dw3@example.com", workspace_name="DW3")

    assert client.get("/links/export.csv", params={"since": "last-tuesday"}).status_code == 422


def test_the_export_audit_records_the_window(client: TestClient):
    """ "What was exported" has to stay answerable after the fact."""
    register_workspace(client, email="dw4@example.com", workspace_name="DW4")
    _add(client, "https://example.com/a.pdf")

    client.get("/links/export.json", params={"since": "2026-01-01", "until": "2026-12-31"})
    rows = [r for r in client.get("/auth/me/export").json()["audit_log"] if r["action"] == "link.export"]

    assert "since=2026-01-01" in rows[0]["detail"]
    assert "until=2026-12-31" in rows[0]["detail"]


# --- POST /links content types (idea 180) ----------------------------------


def test_plain_text_bodies_are_accepted(client: TestClient):
    """The shape a shell one-liner produces without help."""
    register_workspace(client, email="ct1@example.com", workspace_name="CT1")

    response = client.post(
        "/links",
        content=b"see https://peps.python.org/pep-0703/",
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 201
    assert response.json()["stored"] == 1


def test_json_bodies_still_work_exactly_as_before(client: TestClient):
    register_workspace(client, email="ct2@example.com", workspace_name="CT2")

    response = client.post("/links", json={"text": "https://example.com/a.pdf"})

    assert response.status_code == 201
    assert response.json() == {"found": 1, "stored": 1, "duplicates": 0}


def test_both_content_types_enforce_the_same_limits(client: TestClient):
    """Validation lives in one model for both paths, so the size cap and
    the empty-body rejection cannot drift apart."""
    register_workspace(client, email="ct3@example.com", workspace_name="CT3")
    too_long = "https://example.com/" + ("a" * 50_001)

    as_json = client.post("/links", json={"text": too_long})
    as_text = client.post("/links", content=too_long.encode(), headers={"Content-Type": "text/plain"})
    empty_json = client.post("/links", json={"text": ""})
    empty_text = client.post("/links", content=b"", headers={"Content-Type": "text/plain"})

    assert as_json.status_code == as_text.status_code == 422
    assert empty_json.status_code == empty_text.status_code == 422


def test_a_malformed_json_body_carries_a_machine_readable_code(client: TestClient):
    register_workspace(client, email="ct4@example.com", workspace_name="CT4")

    response = client.post("/links", content=b"{not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.MALFORMED_BODY


def test_a_json_array_where_an_object_was_expected_is_rejected(client: TestClient):
    register_workspace(client, email="ct5@example.com", workspace_name="CT5")

    response = client.post("/links", json=["https://example.com/a.pdf"])

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.MALFORMED_BODY


def test_plain_text_still_requires_authentication(client: TestClient):
    response = client.post("/links", content=b"https://example.com/a", headers={"Content-Type": "text/plain"})

    assert response.status_code == 401


def test_the_documented_json_export_shape_is_what_is_served(client: TestClient):
    """Idea 173. A documented schema nobody checks is a wish."""
    register_workspace(client, email="js1@example.com", workspace_name="JS1")
    _add(client, "كتاب https://example.com/a.pdf")

    rows = json.loads(client.get("/links/export.json").text)

    assert isinstance(rows, list)
    required = {"url", "domain", "category", "confidence", "collected_at"}
    assert required <= set(rows[0])


def test_the_csv_export_has_a_stable_header_row(client: TestClient):
    register_workspace(client, email="js2@example.com", workspace_name="JS2")
    _add(client, "https://example.com/a.pdf")

    header = next(csv.reader(io.StringIO(client.get("/links/export.csv").text)))

    assert "url" in header
    assert "category" in header


def test_the_export_format_doc_is_current():
    """Generated from EXPORT_COLUMNS, so a new column with no description
    fails here rather than shipping an undocumented field to whoever built
    an integration on it."""
    result = subprocess.run(
        [sys.executable, "scripts/export_schema.py", "--check"], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_doc_describes_exactly_the_served_columns(client: TestClient):
    """The document and the endpoint compared against each other, not each
    against a list someone typed."""
    register_workspace(client, email="js3@example.com", workspace_name="JS3")
    _add(client, "https://example.com/a.pdf")

    served = set(json.loads(client.get("/links/export.json").text)[0])
    documented = set(re.findall(r"^\| `(\w+)` \|", Path("docs/18-export-format.md").read_text(), re.M))

    assert served <= documented


# --- the importer script, against a real database --------------------------


@pytest.fixture
def workspace_id() -> int:
    with SessionLocal() as db:
        workspace = Workspace(name="Bookmarks WS")
        db.add(workspace)
        db.commit()
        return workspace.id


def test_a_browser_export_becomes_classified_links(tmp_path, workspace_id: int):
    path = tmp_path / "bookmarks.html"
    path.write_text(CHROME_EXPORT, encoding="utf-8")

    summary, skipped = import_bookmarks([path], workspace_id, dry_run=False)

    assert summary.stored == 2
    with SessionLocal() as db:
        urls = {link.url for link in db.query(Link).filter(Link.workspace_id == workspace_id)}
    assert urls == {"https://peps.python.org/pep-0703/", "http://example.com/a"}


def test_several_files_import_in_one_run(tmp_path, workspace_id: int):
    """Idea 178. A person migrating has a browser export and a Pocket
    export, not one file."""
    html = tmp_path / "chrome.html"
    html.write_text(CHROME_EXPORT, encoding="utf-8")
    csv_file = tmp_path / "pocket.csv"
    csv_file.write_text("url,title,time_added\nhttps://example.com/pocket,From Pocket,1750000000\n", "utf-8")

    summary, _ = import_bookmarks([html, csv_file], workspace_id, dry_run=False)

    assert summary.stored == 3
    with SessionLocal() as db:
        channels = {c.title for c in db.query(Channel).filter(Channel.workspace_id == workspace_id)}
    # One channel per file: a browser's bookmarks and a Pocket archive are
    # different collections, and merging them discards the only provenance
    # these formats carry.
    assert channels == {"Bookmarks: chrome.html", "Bookmarks: pocket.csv"}


def test_reimporting_the_same_file_adds_nothing(tmp_path, workspace_id: int):
    path = tmp_path / "bookmarks.html"
    path.write_text(CHROME_EXPORT, encoding="utf-8")

    import_bookmarks([path], workspace_id, dry_run=False)
    second, _ = import_bookmarks([path], workspace_id, dry_run=False)

    assert second.stored == 0
    assert second.duplicates == 2


def test_dry_run_writes_nothing(tmp_path, workspace_id: int):
    path = tmp_path / "bookmarks.html"
    path.write_text(CHROME_EXPORT, encoding="utf-8")

    summary, _ = import_bookmarks([path], workspace_id, dry_run=True)

    assert summary.total_found == 2
    with SessionLocal() as db:
        assert db.query(Link).filter(Link.workspace_id == workspace_id).count() == 0


def test_a_latin1_export_is_read_rather_than_refused(tmp_path, workspace_id: int):
    """Old exports are frequently not UTF-8. One mangled title beats
    refusing the whole file."""
    path = tmp_path / "old.html"
    path.write_bytes('<DL><DT><A HREF="https://example.com/caf">Caf\xe9</A></DL>'.encode("latin-1"))

    summary, _ = import_bookmarks([path], workspace_id, dry_run=False)

    assert summary.stored == 1


def test_importing_into_an_unknown_workspace_stops_with_a_clear_message(tmp_path):
    path = tmp_path / "bookmarks.html"
    path.write_text(CHROME_EXPORT, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        import_bookmarks([path], 999_999, dry_run=False)

    assert "no workspace" in str(exc.value)


def test_a_missing_file_stops_with_a_clear_message(tmp_path, workspace_id: int):
    with pytest.raises(SystemExit) as exc:
        import_bookmarks([tmp_path / "nope.html"], workspace_id, dry_run=False)

    assert "no such file" in str(exc.value)


def test_bookmarklets_never_reach_the_database(tmp_path, workspace_id: int):
    """The end-to-end version of the parser test: not merely filtered in
    memory, but absent from the table afterwards."""
    path = tmp_path / "hostile.html"
    path.write_text(
        '<DL><DT><A HREF="javascript:alert(1)">X</A><DT><A HREF="https://example.com/ok">OK</A></DL>',
        encoding="utf-8",
    )

    _, skipped = import_bookmarks([path], workspace_id, dry_run=False)

    assert skipped.skipped_unsupported_scheme == 1
    with SessionLocal() as db:
        urls = [link.url for link in db.query(Link).filter(Link.workspace_id == workspace_id)]
    assert urls == ["https://example.com/ok"]
    assert not any(u.startswith("javascript:") for u in urls)


def test_the_front_matter_is_valid_yaml_not_merely_yaml_shaped(client: TestClient):
    """Idea 166's promise is that Obsidian and Notion read the block as
    note properties. That means a real parser has to accept it — including
    when the search term contains the quote character that would otherwise
    end the scalar early."""
    register_workspace(client, email="fm3@example.com", workspace_name="FM3")
    _add(client, "https://example.com/a.pdf")

    body = client.get("/links/export.md", params={"q": 'he said "hi" — ok'}).text
    _, block, _ = body.split("---\n", 2)
    parsed = yaml.safe_load(block)

    assert isinstance(parsed, dict)
    assert parsed["source"]
    assert parsed["exported_at"]
    assert "hi" in parsed["query"]


def test_the_documented_vocabularies_are_the_ones_the_code_emits():
    """This test exists because the first draft of the export document
    described `source_type` as "channel/manual/import/bot" — four values
    the code has never produced. The generator guarantees the *field list*
    is real; only a check like this makes the *descriptions* real too."""
    doc = Path("docs/18-export-format.md").read_text(encoding="utf-8")
    source_row = next(line for line in doc.splitlines() if line.startswith("| `source_type`"))

    for emitted in ("text", "hyperlink", "button"):
        assert f"`{emitted}`" in source_row, f"{emitted} is emitted but not documented"

    status_row = next(line for line in doc.splitlines() if line.startswith("| `status_category`"))
    for label in ("ok", "redirect", "blocked", "missing", "throttled", "server_error", "unreachable", "unchecked"):
        assert f"`{label}`" in status_row or label in status_row, f"{label} missing from the doc"


def test_the_documented_status_labels_match_the_vitality_module():
    """Same idea, checked against the code rather than a second list."""
    doc = Path("docs/18-export-format.md").read_text(encoding="utf-8")

    for label in {
        status_category(code, alive)
        for code in (None, 200, 301, 403, 404, 429, 500)
        for alive in (None, True, False)
    }:
        assert label in doc, f"status_category can return {label!r}, which the doc never mentions"
