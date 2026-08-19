"""Tests for the Telegram Desktop export importer.

This is the path that needs no api_id, api_hash, session string or login
code, so it is how the system gets real data when API access is not set up.
"""

from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import Channel, Link, Workspace
from scripts.import_telegram_export import parse_date, parse_message_text, run


@pytest.fixture
def workspace_id() -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Import WS")
        db.add(workspace)
        db.commit()
        return workspace.id
    finally:
        db.close()


def _write_export(tmp_path, messages, name="قناة الأفلام", chat_id=777):
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps({"name": name, "type": "public_channel", "id": chat_id, "messages": messages}),
        encoding="utf-8",
    )
    return path


def test_plain_string_text_is_parsed():
    assert parse_message_text("زوروا https://a.example") == ("زوروا https://a.example", [])


def test_mixed_entity_array_is_flattened():
    text, hidden = parse_message_text(["حمّل من ", {"type": "link", "text": "https://a.example/f.apk"}, " الآن"])
    assert text == "حمّل من https://a.example/f.apk الآن"
    assert hidden == []


def test_hyperlink_hidden_behind_caption_is_recovered():
    """A text_link shows a caption while the real URL sits in href.

    Scanning only the visible text would drop these links entirely.
    """
    text, hidden = parse_message_text(
        ["اضغط ", {"type": "text_link", "text": "هنا", "href": "https://hidden.example/file.pdf"}]
    )
    assert "hidden.example" not in text
    assert hidden == ["https://hidden.example/file.pdf"]


def test_malformed_text_field_does_not_crash():
    assert parse_message_text(None) == ("", [])
    assert parse_message_text(12345) == ("", [])


def test_dates_are_parsed_from_either_field():
    assert parse_date({"date": "2024-01-15T10:30:00"}).year == 2024
    assert parse_date({"date_unixtime": "1705314600"}) is not None
    assert parse_date({}) is None
    assert parse_date({"date": "not-a-date"}) is None


def test_import_stores_classified_links(tmp_path, workspace_id):
    path = _write_export(
        tmp_path,
        [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-15T10:30:00",
                "text": "فيلم https://example.com/movie.mkv",
            },
            {"id": 2, "type": "message", "text": "تطبيق https://example.com/app.apk!"},
            {"id": 3, "type": "service", "text": "انضم أحدهم للقناة"},
            {"id": 4, "type": "message", "text": "رسالة بدون روابط"},
        ],
    )

    summary = run(path, workspace_id, dry_run=False)

    assert summary.stored == 2
    assert summary.duplicates == 0

    db = SessionLocal()
    try:
        links = {link.url: link for link in db.query(Link).all()}
        assert set(links) == {"https://example.com/movie.mkv", "https://example.com/app.apk"}
        assert links["https://example.com/movie.mkv"].category == "movies_series"
        assert links["https://example.com/app.apk"].category == "software_apps"
        assert links["https://example.com/movie.mkv"].posted_at is not None

        channel = db.query(Channel).one()
        assert channel.tg_channel_id == "import:777"
        assert channel.title == "قناة الأفلام"
    finally:
        db.close()


def test_reimporting_the_same_export_adds_nothing(tmp_path, workspace_id):
    path = _write_export(tmp_path, [{"id": 1, "type": "message", "text": "https://example.com/a.apk"}])

    first = run(path, workspace_id, dry_run=False)
    second = run(path, workspace_id, dry_run=False)

    assert (first.stored, first.duplicates) == (1, 0)
    assert (second.stored, second.duplicates) == (0, 1)

    db = SessionLocal()
    try:
        assert db.query(Link).count() == 1
    finally:
        db.close()


def test_dry_run_writes_nothing(tmp_path, workspace_id):
    path = _write_export(tmp_path, [{"id": 1, "type": "message", "text": "https://example.com/a.apk"}])

    summary = run(path, workspace_id, dry_run=True)

    assert summary.stored == 1  # what *would* have been stored
    db = SessionLocal()
    try:
        assert db.query(Link).count() == 0
    finally:
        db.close()


def test_hidden_hyperlinks_are_imported(tmp_path, workspace_id):
    path = _write_export(
        tmp_path,
        [
            {
                "id": 1,
                "type": "message",
                "text": ["الكتاب ", {"type": "text_link", "text": "من هنا", "href": "https://example.com/b.pdf"}],
            }
        ],
    )

    assert run(path, workspace_id, dry_run=False).stored == 1

    db = SessionLocal()
    try:
        assert db.query(Link).one().url == "https://example.com/b.pdf"
    finally:
        db.close()


def test_missing_file_exits_with_a_clear_message(tmp_path, workspace_id):
    with pytest.raises(SystemExit) as exc:
        run(tmp_path / "nope.json", workspace_id, dry_run=True)
    assert "no such file" in str(exc.value)


def test_non_export_json_is_rejected(tmp_path, workspace_id):
    path = tmp_path / "other.json"
    path.write_text('{"something": "else"}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        run(path, workspace_id, dry_run=True)
    assert "does not look like a Telegram export" in str(exc.value)


def test_unknown_workspace_is_rejected(tmp_path):
    path = _write_export(tmp_path, [{"id": 1, "type": "message", "text": "https://a.example"}])

    with pytest.raises(SystemExit) as exc:
        run(path, 999_999, dry_run=True)
    assert "no workspace with id" in str(exc.value)
