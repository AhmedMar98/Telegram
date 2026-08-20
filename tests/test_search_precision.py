"""Per-URL context: a multi-link message must not match as one blob.

Before this, every link in a message stored the *entire* message as its
searchable text, so pasting twenty links under twenty labels meant a
search for any one label returned all twenty. These tests pin the fix and
guard the recall cases that must keep working.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.classifier import extract_url_spans, split_context
from tests.conftest import register_workspace


def _contexts(text: str) -> list[str]:
    return split_context(text, extract_url_spans(text))


# --- the splitter itself ---------------------------------------------------


def test_label_on_the_line_before_its_link_stays_with_it():
    text = "🎬 فيلم المغامرة\nhttps://a.example/movie.mp4\n📚 كتاب البرمجة\nhttps://b.example/book.pdf"

    first, second = _contexts(text)

    assert "فيلم المغامرة" in first
    assert "كتاب البرمجة" not in first
    assert "كتاب البرمجة" in second
    assert "فيلم المغامرة" not in second


def test_label_on_the_same_line_after_its_link_stays_with_it():
    text = "https://a.example/movie.mp4 - فيلم المغامرة\nhttps://b.example/book.pdf - كتاب البرمجة"

    first, second = _contexts(text)

    assert "فيلم المغامرة" in first
    assert "كتاب البرمجة" not in first
    assert "كتاب البرمجة" in second


def test_a_single_link_keeps_the_whole_message():
    """Nothing to disambiguate, so truncating would only lose recall."""
    text = "كتاب مفيد جداً https://only.example/book.pdf أنصح به"

    assert _contexts(text) == [text]


def test_text_before_the_first_and_after_the_last_link_is_kept():
    text = "مقدمة\nhttps://a.example/x\nhttps://b.example/y\nخاتمة"

    first, second = _contexts(text)

    assert "مقدمة" in first
    assert "خاتمة" in second


def test_a_gap_without_a_newline_is_split_on_a_word_boundary():
    """Genuinely ambiguous, so it is split — but never mid-word."""
    text = "https://a.example/x فيلم https://b.example/y"

    first, second = _contexts(text)

    assert first == "https://a.example/x فيلم"
    assert second == "https://b.example/y"


def test_message_with_no_links_yields_no_contexts():
    assert _contexts("لا يوجد أي رابط هنا") == []


# --- end to end through the API -------------------------------------------


def test_searching_a_bulk_paste_returns_only_the_matching_link(client: TestClient):
    """The regression this whole change exists for."""
    register_workspace(client, email="prec@example.com", workspace_name="Precision Co")
    client.post(
        "/links",
        json={
            "text": (
                "🎬 مسلسل الصحراء\nhttps://example.com/desert.mp4\n"
                "🎬 مسلسل الجبال\nhttps://example.com/mountains.mp4\n"
                "🎬 مسلسل البحار\nhttps://example.com/seas.mp4"
            )
        },
    )

    results = client.get("/links", params={"q": "الجبال"}).json()

    assert results["total"] == 1
    assert results["items"][0]["url"] == "https://example.com/mountains.mp4"


def test_a_term_shared_by_every_line_still_returns_every_link(client: TestClient):
    """Splitting must not cost recall on terms that genuinely apply to all."""
    register_workspace(client, email="prec2@example.com", workspace_name="Precision2")
    client.post(
        "/links",
        json={"text": "مسلسل الصحراء\nhttps://example.com/a.mp4\nمسلسل الجبال\nhttps://example.com/b.mp4"},
    )

    assert client.get("/links", params={"q": "مسلسل"}).json()["total"] == 2


def test_context_is_what_gets_stored_not_the_whole_message(client: TestClient):
    register_workspace(client, email="prec3@example.com", workspace_name="Precision3")
    client.post(
        "/links",
        json={"text": "أول\nhttps://example.com/first.pdf\nثاني\nhttps://example.com/second.pdf"},
    )

    items = {i["url"]: i["raw_text"] for i in client.get("/links").json()["items"]}

    assert "ثاني" not in items["https://example.com/first.pdf"]
    assert "أول" not in items["https://example.com/second.pdf"]
