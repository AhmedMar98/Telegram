"""The account-free path: routing a link, and reading a public channel.

The operator's original question was "can I put a link in the web and read
the channel without a userbot". The answer is yes for exactly one shape of
link and no for every other, and the tests that matter here are the ones
that pin the *no* — a router that guesses turns "wrong tool for this link"
into "this channel has no links", which is indistinguishable from success.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.dialogs import SOURCE_PUBLIC, SOURCE_USERBOT
from app.models import Channel, Link
from app.publicsource import classify_source, collect_public_channel, parse_preview
from tests.conftest import register_workspace

# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/python_weekly",
        "http://t.me/python_weekly",
        "t.me/python_weekly",
        "https://www.t.me/python_weekly",
        "https://telegram.me/python_weekly",
        "https://t.me/s/python_weekly",
        "@python_weekly",
        "python_weekly",
    ],
)
def test_every_spelling_of_a_public_channel_routes_to_the_scraper(raw: str):
    """An operator has one thing in the clipboard and should not have to
    know which of eight shapes it is."""
    ref = classify_source(raw)
    assert ref is not None and ref.kind == "public"
    assert ref.value == "python_weekly"


@pytest.mark.parametrize(
    "raw",
    ["https://t.me/+AbcDef12345", "https://t.me/joinchat/AAAAAbCd", "https://t.me/c/1234567890/45"],
)
def test_a_private_link_never_routes_to_the_scraper(raw: str):
    """The scraper cannot read these. Routing one here would fetch a page
    that does not exist and report zero links, which reads as an empty
    channel rather than as the wrong tool."""
    ref = classify_source(raw)
    assert ref is not None and ref.kind == "invite"


def test_a_bare_numeric_id_is_recognised_as_needing_an_account():
    ref = classify_source("-1001234567890")
    assert ref is not None and ref.kind == "id"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "https://example.com/foo", "https://t.me/", "@ab", "https://t.me/s/", "@_bad_"],
)
def test_what_cannot_be_placed_is_refused_rather_than_guessed(raw: str):
    assert classify_source(raw) is None


# --- parsing ---------------------------------------------------------------

PAGE = """
<div class="tgme_widget_message" data-post="chan/102">
  <div class="tgme_widget_message_text js-message_text">
    كتاب مفيد <a href="https://example.com/book.pdf">example.com/bo…</a><br>وثانٍ
  </div>
</div>
<div class="tgme_widget_message" data-post="chan/101">
  <div class="tgme_widget_message_text">بلا روابط هنا</div>
</div>
"""


def test_posts_come_back_oldest_first_with_their_real_ids():
    posts = parse_preview(PAGE)
    assert [p.message_id for p in posts] == [101, 102]


def test_the_href_is_extracted_not_the_shortened_label():
    """Telegram renders a link's visible text truncated with an ellipsis.
    Storing what the reader sees would store a URL that resolves to
    nothing."""
    posts = parse_preview(PAGE)
    newest = [p for p in posts if p.message_id == 102][0]
    assert "https://example.com/book.pdf" in newest.text


def test_a_page_whose_structure_changed_is_skipped_not_mispaired():
    """Post ids and message bodies are paired positionally. If Telegram
    changes the markup so the counts disagree, pairing them anyway would
    attach links to the wrong ids and poison the watermark permanently."""
    assert parse_preview('<div data-post="chan/5"></div>') == []


# --- reading end to end ----------------------------------------------------


def _public_source(client: TestClient, ref: str = "@python_weekly"):
    return client.post("/channels/public", json={"ref": ref})


def test_a_public_channel_can_be_registered_without_any_account(client: TestClient):
    register_workspace(client, email="pub@example.com", workspace_name="Pub")

    response = _public_source(client)

    assert response.status_code == 201
    body = response.json()
    assert body["source"] == SOURCE_PUBLIC
    assert body["account_id"] is None, "a public source must not claim a collector account"


def test_an_invite_link_is_refused_with_a_reason_naming_the_fix(client: TestClient):
    register_workspace(client, email="inv@example.com", workspace_name="Inv")

    response = _public_source(client, "https://t.me/+SecretHash1")

    assert response.status_code == 422
    assert "حساب" in response.json()["detail"], "the refusal must say what would work instead"


def test_gibberish_is_refused_rather_than_registered(client: TestClient):
    register_workspace(client, email="junk@example.com", workspace_name="Junk")
    assert _public_source(client, "https://example.com/nope").status_code == 422


def test_a_channel_already_read_by_a_userbot_is_not_added_a_second_time(client: TestClient):
    """The corruption `source` exists to prevent, tested at the door.

    Two rows for one channel means two watermarks over one history: each
    reader advances its own, and each skips what the other already moved
    past. The result is a permanent hole that raises nothing.
    """
    register_workspace(client, email="dupe@example.com", workspace_name="Dupe")
    assert (
        client.post("/channels", json={"tg_channel_id": "python_weekly", "username": "python_weekly"}).status_code
        == 201
    )

    response = _public_source(client, "@python_weekly")

    assert response.status_code == 409
    assert "حسابات الجمع" in response.json()["detail"]


def test_reading_a_public_source_stores_its_links_and_moves_the_watermark(client: TestClient):
    register_workspace(client, email="read@example.com", workspace_name="Read")
    channel_id = _public_source(client).json()["id"]

    async def fake_fetch(username, *, before=None):
        return PAGE if before is None else None

    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        stored = asyncio.run(collect_public_channel(db, channel, fetch=fake_fetch))
        db.refresh(channel)

        assert stored == 1
        assert channel.last_message_id == 102
        assert channel.last_collected_at is not None

        urls = [row.url for row in db.query(Link).filter(Link.channel_id == channel_id).all()]
        assert urls == ["https://example.com/book.pdf"]
    finally:
        db.close()


def test_a_second_read_collects_nothing_new(client: TestClient):
    """The watermark has to actually hold, or every run re-ingests the same
    page and the duplicate counter is the only thing that grows."""
    register_workspace(client, email="again@example.com", workspace_name="Again")
    channel_id = _public_source(client).json()["id"]

    async def fake_fetch(username, *, before=None):
        return PAGE if before is None else None

    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        asyncio.run(collect_public_channel(db, channel, fetch=fake_fetch))
        second = asyncio.run(collect_public_channel(db, channel, fetch=fake_fetch))

        assert second == 0
    finally:
        db.close()


def test_an_unreachable_source_is_survived_not_raised(client: TestClient):
    """A public channel going private is normal, not exceptional. It must
    not end a run that has other sources to read."""
    register_workspace(client, email="gone@example.com", workspace_name="Gone")
    channel_id = _public_source(client).json()["id"]

    async def dead_fetch(username, *, before=None):
        return None

    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_id)
        assert asyncio.run(collect_public_channel(db, channel, fetch=dead_fetch)) == 0
    finally:
        db.close()


def test_the_userbot_collector_never_picks_up_a_public_row(client: TestClient):
    """The other half of the one-row-one-reader rule, tested where it is
    enforced rather than where it is documented."""
    import scripts.collect as collector
    from app.models import TelegramAccount

    register_workspace(client, email="sep@example.com", workspace_name="Sep")
    _public_source(client)

    db = SessionLocal()
    try:
        channel = db.query(Channel).filter(Channel.source == SOURCE_PUBLIC).one()
        account = TelegramAccount(workspace_id=channel.workspace_id, label="a", session_string="x")
        db.add(account)
        db.commit()

        picked = collector._channels_for(db, channel.workspace_id, account, is_default=True)

        assert channel.id not in [c.id for c in picked]
        assert all(c.source == SOURCE_USERBOT for c in picked)
    finally:
        db.close()
