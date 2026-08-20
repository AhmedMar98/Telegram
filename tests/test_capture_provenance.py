"""Where a link came from, and why it got the category it got.

Before this, a link extracted from an inline-keyboard button, a forwarded
post and a pasted message were indistinguishable once stored, and the rule
that produced the category was computed and thrown away. These tests pin
down the provenance the system now records — and, just as importantly, that
recording it did not change *which* links get captured.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.classifier import detect_language
from app.database import SessionLocal
from app.ingest import MAX_LINKS_PER_MESSAGE, IngestSummary, get_or_create_channel, ingest_text
from app.models import Channel, Link, Workspace
from scripts import collect as collector
from tests.conftest import register_workspace
from tests.test_collector import FakeClient, FakeMessage

# --- language detection ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("تحميل الكتاب الجديد مجاناً", "ar"),
        ("Download the new book for free", "en"),
        ("تحميل الكتاب Download the new free book now please", "mixed"),
        ("", None),
        ("12345 !!! ###", None),
    ],
)
def test_language_is_detected_from_script(text: str, expected: str | None):
    assert detect_language(text) == expected


def test_a_url_does_not_make_an_arabic_message_look_mixed():
    """Every link contributes Latin letters; counting them would label
    essentially every Arabic message "mixed" and make the filter useless."""
    assert detect_language("تحميل الكتاب من هنا https://www.example.com/books/new-arabic-book.pdf") == "ar"


def test_a_single_english_word_does_not_make_a_message_mixed():
    assert detect_language("تحميل تطبيق واتساب الاصدار الجديد كامل مجانا WhatsApp") == "ar"


# --- provenance on the stored row -----------------------------------------


def test_a_pasted_link_is_recorded_as_text(client: TestClient):
    register_workspace(client, email="prov@example.com", workspace_name="Prov")
    client.post("/links", json={"text": "كتاب مفيد https://example.com/book.pdf"})

    item = client.get("/links").json()["items"][0]
    assert item["source_type"] == "text"
    assert item["forwarded_from"] is None
    assert item["language"] == "ar"


def test_the_matched_rule_is_persisted_and_returned(client: TestClient):
    register_workspace(client, email="rule@example.com", workspace_name="Rule")
    client.post("/links", json={"text": "https://example.com/book.pdf"})

    item = client.get("/links").json()["items"][0]
    assert item["category"] == "books_courses"
    # The exact rule string is the classifier's business; what matters here
    # is that a reason exists and is not an empty placeholder.
    assert item["matched_rule"]
    assert "pdf" in item["matched_rule"]


def test_button_and_hyperlink_targets_carry_their_own_source_type():
    db = SessionLocal()
    try:
        workspace = Workspace(name="Kinds WS")
        db.add(workspace)
        db.flush()
        channel = get_or_create_channel(db, workspace_id=workspace.id, tg_channel_id="kinds")
        ingest_text(
            db,
            workspace_id=workspace.id,
            channel_id=channel.id,
            text="شاهد الآن https://example.com/in-body.pdf",
            extra_urls=["https://example.com/hidden.pdf"],
            button_urls=["https://example.com/button.apk"],
        )
        db.commit()
        kinds = {
            link.url: link.source_type for link in db.query(Link).filter(Link.workspace_id == workspace.id).all()
        }
    finally:
        db.close()

    assert kinds == {
        "https://example.com/in-body.pdf": "text",
        "https://example.com/hidden.pdf": "hyperlink",
        "https://example.com/button.apk": "button",
    }


def test_a_url_in_the_body_is_not_duplicated_by_a_button_repeating_it():
    """Telegram posts routinely put the same link in the text and on the
    button; that is one link, and it keeps the body's provenance."""
    db = SessionLocal()
    try:
        workspace = Workspace(name="Dup WS")
        db.add(workspace)
        db.flush()
        channel = get_or_create_channel(db, workspace_id=workspace.id, tg_channel_id="dup")
        summary = ingest_text(
            db,
            workspace_id=workspace.id,
            channel_id=channel.id,
            text="حمل من هنا https://example.com/same.apk",
            button_urls=["https://example.com/same.apk"],
        )
        db.commit()
        rows = db.query(Link).filter(Link.workspace_id == workspace.id).all()
    finally:
        db.close()

    assert summary.stored == 1
    assert [r.source_type for r in rows] == ["text"]


# --- the per-message cap ---------------------------------------------------


def _ingest_many(count: int) -> tuple[IngestSummary, int]:
    db = SessionLocal()
    try:
        workspace = Workspace(name=f"Cap WS {count}")
        db.add(workspace)
        db.flush()
        channel = get_or_create_channel(db, workspace_id=workspace.id, tg_channel_id=f"cap{count}")
        text = " ".join(f"https://example.com/{i}.pdf" for i in range(count))
        summary = ingest_text(db, workspace_id=workspace.id, channel_id=channel.id, text=text)
        db.commit()
        return summary, db.query(Link).filter(Link.workspace_id == workspace.id).count()
    finally:
        db.close()


def test_a_message_under_the_cap_is_stored_whole():
    summary, stored = _ingest_many(MAX_LINKS_PER_MESSAGE)
    assert stored == MAX_LINKS_PER_MESSAGE
    assert summary.truncated_messages == 0
    assert summary.dropped_links == 0


def test_a_link_dump_is_capped_and_the_loss_is_counted():
    """Silently dropping links would be worse than the dump itself: the
    caller has to be able to report what was not stored."""
    over = MAX_LINKS_PER_MESSAGE + 7
    summary, stored = _ingest_many(over)

    assert stored == MAX_LINKS_PER_MESSAGE
    assert summary.truncated_messages == 1
    assert summary.dropped_links == 7


# --- the collector's side --------------------------------------------------


class FakeButton:
    def __init__(self, url: str | None):
        self.url = url


class FakeRow:
    def __init__(self, *buttons: FakeButton):
        self.buttons = list(buttons)


class FakeMarkup:
    def __init__(self, *rows: FakeRow):
        self.rows = list(rows)


class FakeForward:
    def __init__(self, *, from_name: str | None = None, chat: object | None = None):
        self.from_name = from_name
        self.chat = chat
        self.sender = None


class FakeChat:
    def __init__(self, title: str):
        self.title = title


@pytest.fixture
def workspace_and_channel():
    """A workspace with one channel. Defined here rather than imported from
    ``test_collector``: importing a fixture across modules works only by
    accident of import order, and pytest gives no error when it silently
    does not."""
    db = SessionLocal()
    try:
        workspace = Workspace(name="Provenance WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="2002", username="provchan", title="Prov")
        db.add(channel)
        db.commit()
        return workspace.id, channel.id
    finally:
        db.close()


def _run_collect(client, channel_id: int) -> int:
    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        return asyncio.run(collector._collect_channel(client, db, channel))
    finally:
        db.close()


def test_a_link_that_exists_only_on_a_button_is_collected(workspace_and_channel):
    """The whole point: bot posts put the real download link on a button and
    leave the body as marketing copy, so a body-only collector stores nothing."""
    workspace_id, channel_id = workspace_and_channel
    message = FakeMessage(9, "اضغط الزر بالأسفل للتحميل")
    message.reply_markup = FakeMarkup(FakeRow(FakeButton("https://example.com/from-button.apk")))

    assert _run_collect(FakeClient([message]), channel_id) == 1

    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.workspace_id == workspace_id).one()
        assert link.url == "https://example.com/from-button.apk"
        assert link.source_type == "button"
    finally:
        db.close()


def test_non_url_buttons_are_ignored(workspace_and_channel):
    """Callback and switch-inline buttons have no ``url``; reading one
    blindly would raise and abort the whole channel's collection."""
    workspace_id, channel_id = workspace_and_channel
    message = FakeMessage(3, "https://example.com/body.pdf")
    message.reply_markup = FakeMarkup(FakeRow(FakeButton(None), FakeButton("tg://resolve?domain=x")))

    assert _run_collect(FakeClient([message]), channel_id) == 1

    db = SessionLocal()
    try:
        urls = [link.url for link in db.query(Link).filter(Link.workspace_id == workspace_id).all()]
    finally:
        db.close()
    assert urls == ["https://example.com/body.pdf"]


def test_a_message_with_no_reply_markup_still_collects(workspace_and_channel):
    """``reply_markup`` is absent on most messages — the common case must
    not depend on it existing."""
    _, channel_id = workspace_and_channel
    assert _run_collect(FakeClient([FakeMessage(2, "https://example.com/plain.pdf")]), channel_id) == 1


@pytest.mark.parametrize(
    ("forward", "expected"),
    [
        (FakeForward(from_name="قناة الكتب"), "قناة الكتب"),
        (FakeForward(chat=FakeChat("Books Channel")), "Books Channel"),
        (None, None),
    ],
)
def test_forward_origin_is_recorded(workspace_and_channel, forward, expected):
    workspace_id, channel_id = workspace_and_channel
    message = FakeMessage(11, "https://example.com/forwarded.pdf")
    message.forward = forward

    assert _run_collect(FakeClient([message]), channel_id) == 1

    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.workspace_id == workspace_id).one()
        assert link.forwarded_from == expected
    finally:
        db.close()


# --- the language filter ---------------------------------------------------


def test_search_can_be_filtered_by_language(client: TestClient):
    register_workspace(client, email="lang@example.com", workspace_name="Lang")
    client.post("/links", json={"text": "كتاب عربي مفيد جدا https://example.com/arabic.pdf"})
    client.post("/links", json={"text": "A very useful english book https://example.com/english.pdf"})

    arabic = client.get("/links", params={"language": "ar"}).json()
    english = client.get("/links", params={"language": "en"}).json()

    assert [i["url"] for i in arabic["items"]] == ["https://example.com/arabic.pdf"]
    assert [i["url"] for i in english["items"]] == ["https://example.com/english.pdf"]


def test_an_unknown_language_matches_nothing_rather_than_erroring(client: TestClient):
    register_workspace(client, email="lang2@example.com", workspace_name="Lang2")
    client.post("/links", json={"text": "كتاب https://example.com/x.pdf"})

    resp = client.get("/links", params={"language": "zz"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_the_language_filter_is_workspace_scoped(client: TestClient):
    register_workspace(client, email="lang3@example.com", workspace_name="Lang3")
    client.post("/links", json={"text": "كتاب عربي https://example.com/theirs.pdf"})
    client.post("/auth/logout")

    register_workspace(client, email="lang4@example.com", workspace_name="Lang4")
    assert client.get("/links", params={"language": "ar"}).json()["total"] == 0
