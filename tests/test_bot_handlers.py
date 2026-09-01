"""Tests for the Telegram bot's message handlers.

These call the actual handler coroutines directly against a real database
session — the same functions aiogram's dispatcher would invoke on a real
webhook update — rather than standing up the whole aiogram machinery.
That is enough to exercise every real code path (link generation, chat
resolution, the DB queries) without a live Telegram connection, and it
covers a module that previously had zero test coverage despite writing
to the database on every command.
"""

from __future__ import annotations

import asyncio

from app.bot import telegram_bot as bot
from app.database import SessionLocal
from app.ingest import get_or_create_channel, ingest_text
from app.models import BotLink, BotLinkCode, Workspace


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    """Records every call to .answer() instead of hitting the Telegram API."""

    def __init__(self, chat_id: int, text: str = ""):
        self.chat = FakeChat(chat_id)
        self.text = text
        self.sent: list[str] = []
        # Inline keyboards are part of the reply now, so they are captured
        # rather than swallowed — a pager that stops appearing would
        # otherwise pass every assertion about the text.
        self.markups: list[object] = []

    async def answer(self, text: str, reply_markup: object = None, **kwargs: object) -> None:
        # **kwargs so the double keeps accepting what the real Message
        # accepts. It used to take exactly two arguments, and adding
        # disable_web_page_preview to a call site broke eight tests that
        # have nothing to do with previews — a double narrower than the
        # thing it stands in for fails on changes it should not notice.
        self.sent.append(text)
        self.markups.append(reply_markup)

    @property
    def all_text(self) -> str:
        """Everything the bot said, joined.

        Results used to arrive as one numbered block, so asserting on
        ``sent[0]`` worked by accident. Each result is now its own message
        because Telegram attaches a keyboard to a message and not to a
        line inside one — so "did the bot mention this link" is a question
        about the whole reply, not about its first line.
        """
        return "\n".join(self.sent)

    @property
    def keyboards(self) -> list[object]:
        """Only the messages that actually carried buttons."""
        return [m for m in self.markups if m is not None]


class FakeCommand:
    def __init__(self, args: str | None):
        self.args = args


def _workspace() -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Bot WS")
        db.add(workspace)
        db.commit()
        return workspace.id
    finally:
        db.close()


def test_start_without_code_tells_an_unlinked_chat_where_to_get_one():
    """It must name where the code comes from, not just refuse.

    The old reply pointed at "صفحة الإعدادات" — a page by that name does
    not exist. The dashboard's section is «البوت», and a real user spent an
    evening not finding it.
    """
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=111)
        asyncio.run(bot.handle_start(message, FakeCommand(args=None), db))
    finally:
        db.close()

    reply = message.all_text
    assert "البوت" in reply, f"the reply must name the dashboard section: {reply!r}"
    assert "/start" in reply, "it must show the command shape, since a code has to follow it"
    assert message.keyboards == [], "an unlinked chat has nothing to offer buttons for"


def test_start_without_code_gives_a_linked_chat_the_menu():
    """A bare /start from a linked chat is someone opening the bot.

    Before this it got the same "send me a code" text as a stranger, which
    is both wrong and the reason ten commands had to be memorised: there
    was no surface that listed what the bot could do.
    """
    workspace_id = _linked(2101)
    assert workspace_id

    message = FakeMessage(chat_id=2101)
    _run(bot.handle_start, message, FakeCommand(args=None))

    assert message.keyboards, "a linked chat must be offered the menu"
    labels = [b.text for m in message.keyboards for row in m.inline_keyboard for b in row]
    assert any("الأحدث" in t for t in labels), f"menu is missing its actions: {labels}"
    assert any("المفضّلة" in t for t in labels), f"menu is missing its actions: {labels}"


def test_start_with_valid_code_links_the_chat():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        message = FakeMessage(chat_id=222)
        asyncio.run(bot.handle_start(message, FakeCommand(args=code), db))
    finally:
        db.close()

    assert "نجاح" in message.all_text or "✅" in message.all_text
    db = SessionLocal()
    try:
        link = db.get(BotLink, "222")
        assert link is not None
        assert link.workspace_id == workspace_id
        # The code is single-use.
        record = db.query(BotLinkCode).filter(BotLinkCode.code == code).one()
        assert record.used_at is not None
    finally:
        db.close()


def test_start_with_invalid_code_is_rejected():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=333)
        asyncio.run(bot.handle_start(message, FakeCommand(args="not-a-real-code"), db))
    finally:
        db.close()
    assert "غير صالح" in message.all_text


def test_start_with_already_used_code_is_rejected():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        asyncio.run(bot.handle_start(FakeMessage(chat_id=444), FakeCommand(args=code), db))
        # Reusing the same code from a different chat must fail.
        second = FakeMessage(chat_id=555)
        asyncio.run(bot.handle_start(second, FakeCommand(args=code), db))
    finally:
        db.close()
    assert "غير صالح" in second.all_text


def test_search_without_linked_chat_prompts_to_link():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=666)
        asyncio.run(bot.handle_search(message, FakeCommand(args="anything"), db))
    finally:
        db.close()
    assert "غير مرتبطة" in message.all_text


def test_search_without_query_shows_usage():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="777", workspace_id=workspace_id))
        db.commit()
        message = FakeMessage(chat_id=777)
        asyncio.run(bot.handle_search(message, FakeCommand(args=""), db))
    finally:
        db.close()
    assert "الاستخدام" in message.all_text


def test_search_finds_matching_links():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="888", workspace_id=workspace_id))
        channel = get_or_create_channel(db, workspace_id=workspace_id, tg_channel_id="bot-test")
        ingest_text(
            db, workspace_id=workspace_id, channel_id=channel.id, text="https://example.com/python-guide.pdf"
        )
        db.commit()
        message = FakeMessage(chat_id=888)
        asyncio.run(bot.handle_search(message, FakeCommand(args="python"), db))
    finally:
        db.close()
    assert "python-guide" in message.all_text


def test_search_reports_no_results():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="999", workspace_id=workspace_id))
        db.commit()
        message = FakeMessage(chat_id=999)
        asyncio.run(bot.handle_search(message, FakeCommand(args="nothing-will-match-this"), db))
    finally:
        db.close()
    assert "لا نتائج" in message.all_text


def test_search_is_scoped_to_the_linked_workspace():
    """A chat linked to workspace A must never see workspace B's links."""
    workspace_a = _workspace()
    workspace_b = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="1001", workspace_id=workspace_a))
        channel_b = get_or_create_channel(db, workspace_id=workspace_b, tg_channel_id="other-ws")
        ingest_text(db, workspace_id=workspace_b, channel_id=channel_b.id, text="https://example.com/secret.pdf")
        db.commit()
        message = FakeMessage(chat_id=1001)
        asyncio.run(bot.handle_search(message, FakeCommand(args="secret"), db))
    finally:
        db.close()
    assert "لا نتائج" in message.all_text


def test_stats_without_linked_chat_prompts_to_link():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=1111)
        asyncio.run(bot.handle_stats(message, db))
    finally:
        db.close()
    assert "غير مرتبطة" in message.all_text


def test_stats_reports_counts():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="1212", workspace_id=workspace_id))
        channel = get_or_create_channel(db, workspace_id=workspace_id, tg_channel_id="stats-test")
        ingest_text(db, workspace_id=workspace_id, channel_id=channel.id, text="https://example.com/a.apk")
        db.commit()
        message = FakeMessage(chat_id=1212)
        asyncio.run(bot.handle_stats(message, db))
    finally:
        db.close()
    assert "1" in message.all_text


def test_unlinked_chat_gets_linking_instructions_not_a_command_list():
    """A command list is useless to someone who cannot run any of them;
    what they need is how to link the chat."""
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=1313, text="مرحبا")
        asyncio.run(bot.handle_other(message, db))
    finally:
        db.close()
    assert "رمز" in message.all_text
    assert "/start" in message.all_text


def test_a_linked_chat_gets_the_command_list():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="1314", workspace_id=workspace_id))
        db.commit()
        message = FakeMessage(chat_id=1314, text="/")
        asyncio.run(bot.handle_other(message, db))
    finally:
        db.close()
    assert "/search" in message.all_text
    assert "/stats" in message.all_text


def test_get_bot_returns_none_without_token(monkeypatch):
    settings = bot.get_settings()
    monkeypatch.setattr(settings, "bot_token", None)
    assert bot.get_bot() is None


def test_get_bot_returns_a_bot_instance_when_configured(monkeypatch):
    settings = bot.get_settings()
    monkeypatch.setattr(settings, "bot_token", "123456:fake-token-for-testing")
    instance = bot.get_bot()
    assert instance is not None


def test_generate_link_code_is_unique_per_call():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        first = bot.generate_link_code(db, workspace_id)
        second = bot.generate_link_code(db, workspace_id)
        assert first != second
    finally:
        db.close()


# --- Phase 4B: the commands added on top of /start, /search, /stats -------


def _linked(chat_id: int) -> int:
    """A workspace with this chat already linked to it."""
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id=str(chat_id), workspace_id=workspace_id))
        db.commit()
    finally:
        db.close()
    return workspace_id


def _seed(workspace_id: int, texts: list[str]) -> None:
    db = SessionLocal()
    try:
        channel = get_or_create_channel(db, workspace_id=workspace_id, tg_channel_id=f"seed-{workspace_id}")
        for index, text in enumerate(texts):
            ingest_text(db, workspace_id=workspace_id, channel_id=channel.id, text=text, message_id=index)
        db.commit()
    finally:
        db.close()


def _run(handler, message, *args) -> None:
    db = SessionLocal()
    try:
        asyncio.run(handler(message, *args, db))
    finally:
        db.close()


def test_the_bot_and_the_web_agree_on_what_a_search_returns():
    """The bot used to write its own ILIKE instead of using the shared
    query builder, so on Postgres it silently skipped full-text search,
    ranking, exclusion terms and the archived filter. Same builder now."""
    from app.database import SessionLocal as _S
    from app.linkquery import filtered_links

    workspace_id = _linked(2001)
    _seed(
        workspace_id,
        ["دورة بايثون مجانية https://a.example/free.pdf", "دورة بايثون مدفوعة https://b.example/paid.pdf"],
    )

    message = FakeMessage(chat_id=2001)
    _run(bot.handle_search, message, FakeCommand(args="بايثون -مدفوعة"))

    db = _S()
    try:
        expected = {
            link.url for link in filtered_links(db, workspace_id, q="بايثون -مدفوعة", category=None)[0].all()
        }
    finally:
        db.close()

    assert expected == {"https://a.example/free.pdf"}
    assert "free.pdf" in message.all_text
    assert "paid.pdf" not in message.all_text


def test_latest_lists_newest_first():
    workspace_id = _linked(2002)
    _seed(workspace_id, ["https://example.com/old.pdf", "https://example.com/new.pdf"])

    message = FakeMessage(chat_id=2002)
    _run(bot.handle_latest, message)

    body = message.all_text
    assert body.index("new.pdf") < body.index("old.pdf")


def test_favorite_only_returns_starred_links():
    from app.models import Link

    workspace_id = _linked(2003)
    _seed(workspace_id, ["https://example.com/plain.pdf", "https://example.com/starred.pdf"])
    db = SessionLocal()
    try:
        db.query(Link).filter(Link.url.like("%starred%")).update({"is_favorite": True}, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    message = FakeMessage(chat_id=2003)
    _run(bot.handle_favorite, message, FakeCommand(args=""))

    assert "starred.pdf" in message.all_text
    assert "plain.pdf" not in message.all_text


def test_vitality_reports_all_three_states():
    from app.models import Link

    workspace_id = _linked(2004)
    _seed(workspace_id, ["https://example.com/a.pdf", "https://example.com/b.pdf", "https://example.com/c.pdf"])
    db = SessionLocal()
    try:
        db.query(Link).filter(Link.url.like("%a.pdf")).update({"is_alive": True}, synchronize_session=False)
        db.query(Link).filter(Link.url.like("%b.pdf")).update({"is_alive": False}, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    message = FakeMessage(chat_id=2004)
    _run(bot.handle_vitality, message)

    assert "🟢 حيّة: 1" in message.all_text
    assert "🔴 ميتة: 1" in message.all_text
    assert "⚪ لم تُفحص: 1" in message.all_text


def test_channels_lists_the_workspace_channels():
    workspace_id = _linked(2005)
    _seed(workspace_id, ["https://example.com/a.pdf"])

    message = FakeMessage(chat_id=2005)
    _run(bot.handle_channels, message)

    assert f"seed-{workspace_id}" in message.all_text


def test_details_addresses_a_link_by_its_position():
    workspace_id = _linked(2006)
    _seed(workspace_id, ["https://example.com/first.pdf", "https://example.com/second.pdf"])

    message = FakeMessage(chat_id=2006)
    _run(bot.handle_details, message, FakeCommand(args="1"))

    # Newest first, so position 1 is the last one ingested.
    assert "second.pdf" in message.all_text
    assert "التصنيف:" in message.all_text
    assert "القاعدة:" in message.all_text


def test_details_rejects_a_non_number():
    workspace_id = _linked(2007)
    _seed(workspace_id, ["https://example.com/a.pdf"])

    message = FakeMessage(chat_id=2007)
    _run(bot.handle_details, message, FakeCommand(args="drop table"))

    assert "الاستخدام" in message.all_text


def test_details_out_of_range_is_a_plain_miss():
    """Must not leak whether some other workspace has that many links."""
    workspace_id = _linked(2008)
    _seed(workspace_id, ["https://example.com/a.pdf"])

    message = FakeMessage(chat_id=2008)
    _run(bot.handle_details, message, FakeCommand(args="99"))

    assert "لا يوجد رابط بهذا الرقم" in message.all_text


def test_unlink_detaches_the_chat_without_deleting_data():
    from app.models import Link

    workspace_id = _linked(2009)
    _seed(workspace_id, ["https://example.com/a.pdf"])

    message = FakeMessage(chat_id=2009)
    _run(bot.handle_unlink, message)

    db = SessionLocal()
    try:
        assert db.get(BotLink, "2009") is None
        assert db.query(Link).filter(Link.workspace_id == workspace_id).count() == 1
    finally:
        db.close()


def test_unlinking_an_unlinked_chat_is_not_an_error():
    message = FakeMessage(chat_id=2010)
    _run(bot.handle_unlink, message)
    assert "غير مرتبطة" in message.all_text


def test_a_pasted_link_is_saved_without_a_command():
    from app.models import Link

    workspace_id = _linked(2011)

    message = FakeMessage(chat_id=2011, text="شوف هذا https://example.com/pasted.pdf")
    _run(bot.handle_other, message)

    db = SessionLocal()
    try:
        urls = [link.url for link in db.query(Link).filter(Link.workspace_id == workspace_id).all()]
    finally:
        db.close()
    assert urls == ["https://example.com/pasted.pdf"]
    assert "أُضيف 1" in message.all_text


def test_a_bare_phrase_is_treated_as_a_search():
    workspace_id = _linked(2012)
    _seed(workspace_id, ["كتاب الشبكات https://example.com/networks.pdf"])

    message = FakeMessage(chat_id=2012, text="الشبكات")
    _run(bot.handle_other, message)

    assert "networks.pdf" in message.all_text


def test_help_lists_every_registered_command():
    """The help text is built from COMMANDS, so a command added without a
    line there would be undiscoverable."""
    workspace_id = _linked(2013)
    _seed(workspace_id, [])

    message = FakeMessage(chat_id=2013)
    _run(bot.handle_help, message)

    for name, _ in bot.COMMANDS:
        assert name in message.all_text, f"{name} missing from /help"


def test_every_documented_command_has_a_handler():
    """The other direction: a line in COMMANDS with no handler behind it
    advertises something that does nothing."""
    handlers = {
        "/search": bot.handle_search,
        "/latest": bot.handle_latest,
        "/favorite": bot.handle_favorite,
        "/details": bot.handle_details,
        "/stats": bot.handle_stats,
        "/vitality": bot.handle_vitality,
        "/channels": bot.handle_channels,
        "/unlink": bot.handle_unlink,
        "/help": bot.handle_help,
    }
    assert {name for name, _ in bot.COMMANDS} == set(handlers)


def test_a_single_page_of_results_carries_no_pager():
    workspace_id = _linked(2014)
    _seed(workspace_id, ["https://example.com/only.pdf"])

    message = FakeMessage(chat_id=2014)
    _run(bot.handle_latest, message)

    # Results now carry their own details/favourite buttons, so "no pager"
    # can no longer mean "no keyboard anywhere". The pager is the keyboard
    # holding a next/previous label; asserting on a positional index would
    # pass for the wrong reason the moment the message order changes.
    labels = [b.text for m in message.keyboards for row in m.inline_keyboard for b in row]
    assert not any("التالي" in t or "السابق" in t for t in labels), (
        f"one page of results must offer no paging controls: {labels}"
    )


def test_more_than_one_page_carries_a_next_button():
    workspace_id = _linked(2015)
    _seed(workspace_id, [f"https://example.com/{i}.pdf" for i in range(bot.PAGE_SIZE + 2)])

    message = FakeMessage(chat_id=2015)
    _run(bot.handle_latest, message)

    labels = [b.text for m in message.keyboards for row in m.inline_keyboard for b in row]
    assert any("التالي" in t for t in labels), f"a second page exists, so a next control must be offered: {labels}"
    assert not any("السابق" in t for t in labels), "page 0 must not offer a previous page"


def test_the_pager_payload_round_trips():
    """Telegram callbacks arrive with no memory of what produced them, so
    the filter has to survive encoding into 64 bytes and back."""
    token = bot._encode_filter("بايثون -مدفوع", True, "books_courses")
    assert len(token.encode("utf-8")) < 64
    assert bot._decode_filter(token) == ("بايثون -مدفوع", True, "books_courses")


def test_the_pager_payload_survives_an_empty_filter():
    assert bot._decode_filter(bot._encode_filter(None, None, None)) == (None, None, None)


def test_every_command_refuses_an_unlinked_chat():
    """One missed check is a cross-tenant read, so this is asserted over
    the whole command surface rather than one command at a time."""
    for handler, args in (
        (bot.handle_latest, ()),
        (bot.handle_stats, ()),
        (bot.handle_vitality, ()),
        (bot.handle_channels, ()),
        (bot.handle_favorite, (FakeCommand(args=""),)),
        (bot.handle_details, (FakeCommand(args="1"),)),
        (bot.handle_search, (FakeCommand(args="x"),)),
    ):
        message = FakeMessage(chat_id=9999)
        _run(handler, message, *args)
        assert "غير مرتبطة" in message.all_text, f"{handler.__name__} answered an unlinked chat"


class FakeCallback:
    """A callback query, with the message it came from."""

    def __init__(self, data: str, message: object):
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _inaccessible(chat_id: int):
    """A real aiogram InaccessibleMessage — what Telegram sends when the
    original message is too old for the bot to read back. Built from the
    library's own type rather than a stand-in, because the handler's whole
    job here is to recognise that exact type."""
    from aiogram.types import Chat, InaccessibleMessage

    return InaccessibleMessage(chat=Chat(id=chat_id, type="private"), message_id=1, date=0)


def test_paging_returns_the_next_page():
    workspace_id = _linked(2016)
    _seed(workspace_id, [f"https://example.com/{i}.pdf" for i in range(bot.PAGE_SIZE + 2)])

    message = FakeMessage(chat_id=2016)
    callback = FakeCallback(data=f"pg:1:{bot._encode_filter(None, None, None)}", message=message)
    db = SessionLocal()
    try:
        asyncio.run(bot.handle_page(callback, db))
    finally:
        db.close()

    assert "النتائج 6" in message.all_text
    # The spinner on the button only stops once the callback is answered.
    assert callback.answers


def test_paging_an_inaccessible_message_explains_instead_of_crashing():
    """callback.message is Message | InaccessibleMessage | None. Answering
    through the inaccessible variant means an API call about a message the
    bot cannot access, so the user gets an explanation and the button stops
    spinning instead."""
    _linked(2017)
    callback = FakeCallback(
        data=f"pg:1:{bot._encode_filter(None, None, None)}",
        message=_inaccessible(2017),
    )
    db = SessionLocal()
    try:
        asyncio.run(bot.handle_page(callback, db))
    finally:
        db.close()

    assert callback.answers, "the callback was never answered"
    text, show_alert = callback.answers[0]
    assert "أقدم" in (text or "")
    assert show_alert is True


def test_paging_from_an_unlinked_chat_is_refused():
    callback = FakeCallback(data=f"pg:1:{bot._encode_filter(None, None, None)}", message=FakeMessage(chat_id=2018))
    db = SessionLocal()
    try:
        asyncio.run(bot.handle_page(callback, db))
    finally:
        db.close()

    assert any("غير مرتبطة" in (t or "") for t, _ in callback.answers)


# --- the number beside a result has to address that result ----------------
#
# Every test below fails against the code as it stood before this section
# was written. That is the point of them: the 88 bot tests that already
# existed all passed while /details answered about a different link than
# the one it was asked about.


def test_details_after_a_search_returns_the_link_that_was_numbered():
    """The defect, stated as a test.

    ``answer_results`` numbers each line by its position *within the active
    filter*. ``/details`` re-queried with no filter at all, so after any
    search the number on screen and the number in the command addressed two
    different links — and the bot answered confidently with the wrong one.
    """
    workspace_id = _linked(2401)
    # Ingested oldest-first, so "newest" is the last one. Only the two
    # marked "بحثي" match the search.
    _seed(
        workspace_id,
        [
            "قديم https://old.example/1.pdf",
            "بحثي أول https://match.example/first.pdf",
            "حشو https://filler.example/2.pdf",
            "بحثي ثاني https://match.example/second.pdf",
        ],
    )

    listing = FakeMessage(chat_id=2401)
    _run(bot.handle_search, listing, FakeCommand(args="بحثي"))

    # Newest first: 1. second.pdf, 2. first.pdf
    assert "match.example/second.pdf" in listing.sent[1]
    assert "match.example/first.pdf" in listing.sent[2]

    details = FakeMessage(chat_id=2401)
    _run(bot.handle_details, details, FakeCommand(args="2"))

    assert "match.example/first.pdf" in details.all_text, (
        f"/details 2 must return the link printed as 2, not {details.all_text!r}"
    )
    assert "filler.example" not in details.all_text


def test_details_after_a_favourites_listing_stays_inside_the_favourites():
    """The same defect through a different filter, so a fix that special-cased
    the search term alone would not pass."""
    workspace_id = _linked(2402)
    # Seeded so the starred link is the OLDER one. If it were newest, the
    # unfiltered query would return it too and the test would pass against
    # the very bug it exists to catch.
    _seed(workspace_id, ["https://starred.example/b.pdf", "https://plain.example/a.pdf"])

    db = SessionLocal()
    try:
        from app.models import Link

        starred = db.query(Link).filter(Link.url.like("%starred%")).one()
        starred.is_favorite = True
        db.commit()
    finally:
        db.close()

    listing = FakeMessage(chat_id=2402)
    _run(bot.handle_favorite, listing, FakeCommand(args=None))
    assert "starred.example" in listing.all_text

    details = FakeMessage(chat_id=2402)
    _run(bot.handle_details, details, FakeCommand(args="1"))

    assert "starred.example" in details.all_text, (
        f"/details 1 after /favorite must stay in the favourites: {details.all_text!r}"
    )


def test_a_search_that_matches_nothing_clears_the_previous_context():
    """Otherwise the next /details answers about a list the user has left."""
    workspace_id = _linked(2403)
    # kept.example is the NEWEST link, so an unfiltered /details 1 would
    # return it — which is what makes this assertion able to fail.
    _seed(workspace_id, ["https://other.example/b.pdf", "مطابق https://kept.example/a.pdf"])

    _run(bot.handle_search, FakeMessage(chat_id=2403), FakeCommand(args="مطابق"))
    _run(bot.handle_search, FakeMessage(chat_id=2403), FakeCommand(args="لاشيءيطابقهذا"))

    details = FakeMessage(chat_id=2403)
    _run(bot.handle_details, details, FakeCommand(args="1"))

    assert "kept.example" not in details.all_text, "the empty search left the previous search's context in place"


def test_details_with_no_search_yet_means_the_newest_link():
    """The unfiltered list is a real state, not a missing one — a chat that
    has only just linked must still be able to say /details 1."""
    workspace_id = _linked(2404)
    _seed(workspace_id, ["https://older.example/a.pdf", "https://newest.example/b.pdf"])

    details = FakeMessage(chat_id=2404)
    _run(bot.handle_details, details, FakeCommand(args="1"))

    assert "newest.example" in details.all_text


def test_a_long_search_term_survives_into_page_two():
    """The filter used to ride inside callback_data, which Telegram caps at
    64 bytes, so the term was truncated to 30 characters. Page 2 of a longer
    search was a page of a *different* search and nothing said so."""
    workspace_id = _linked(2405)
    term = "المصطلح_الطويل_جدا_الذي_يتجاوز_ثلاثين_حرفا_بسهولة"
    assert len(term) > 30

    _seed(workspace_id, [f"{term} https://match.example/{i}.pdf" for i in range(bot.PAGE_SIZE + 2)])
    # Decoys that match the TRUNCATED term but not the full one. If the
    # truncation is back, these leak into the results.
    _seed(workspace_id, [f"{term[:30]} https://decoy.example/{i}.pdf" for i in range(3)])

    first = FakeMessage(chat_id=2405)
    _run(bot.handle_search, first, FakeCommand(args=term))
    assert "decoy.example" not in first.all_text

    second = FakeMessage(chat_id=2405)
    callback = FakeCallback(data="pg:1", message=second)
    db = SessionLocal()
    try:
        asyncio.run(bot.handle_page(callback, db))
    finally:
        db.close()

    assert "decoy.example" not in second.all_text, (
        f"page 2 ran a truncated version of the query: {second.all_text!r}"
    )


def test_an_old_pager_button_still_carries_its_own_filter():
    """Buttons already sent to real chats have the three-part payload. They
    must keep working rather than silently paging something else."""
    workspace_id = _linked(2406)
    _seed(workspace_id, [f"هدف https://match.example/{i}.pdf" for i in range(bot.PAGE_SIZE + 2)])
    _seed(workspace_id, ["https://unrelated.example/x.pdf"])

    message = FakeMessage(chat_id=2406)
    callback = FakeCallback(data=f"pg:1:{bot._encode_filter('هدف', None, None)}", message=message)
    db = SessionLocal()
    try:
        asyncio.run(bot.handle_page(callback, db))
    finally:
        db.close()

    assert "unrelated.example" not in message.all_text


# --- callback payloads are client-supplied --------------------------------


def test_a_malformed_callback_payload_is_refused_rather_than_raising():
    """int() on client-supplied text raised ValueError out of the handler,
    which aiogram turns into a 500 on the webhook — and Telegram then
    retries the delivery, so one crafted payload becomes many."""
    _linked(2410)

    for payload in ("pg:abc", "pg", "d:abc", "d:", "f:abc:1", "f:12"):
        message = FakeMessage(chat_id=2410)
        callback = FakeCallback(data=payload, message=message)
        handler = {
            "p": bot.handle_page,
            "d": bot.handle_details_button,
            "f": bot.handle_favourite_button,
        }[payload[0]]
        db = SessionLocal()
        try:
            asyncio.run(handler(callback, db))
        finally:
            db.close()

        assert callback.answers, f"{payload!r} was never answered"


# --- a link code is a bearer credential -----------------------------------


def test_a_stale_link_code_no_longer_links_a_chat():
    """The only condition used to be "unused", so a code generated a year
    ago and left visible in a chat log still granted read access to the
    whole workspace."""
    from datetime import timedelta

    from app.bot.routers.onboarding import LINK_CODE_TTL
    from app.timeutil import utcnow

    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        record = db.query(BotLinkCode).filter(BotLinkCode.code == code).one()
        record.created_at = utcnow() - LINK_CODE_TTL - timedelta(minutes=1)
        db.commit()

        message = FakeMessage(chat_id=2420)
        asyncio.run(bot.handle_start(message, FakeCommand(args=code), db))
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.get(BotLink, "2420") is None, "an expired code linked the chat"
    finally:
        db.close()
    assert "صالح" in message.all_text or "الصلاحية" in message.all_text


def test_a_fresh_link_code_still_works():
    """The guard above must not have made every code useless."""
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        message = FakeMessage(chat_id=2421)
        asyncio.run(bot.handle_start(message, FakeCommand(args=code), db))
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.get(BotLink, "2421") is not None
    finally:
        db.close()


def test_the_rejection_does_not_say_which_kind_of_bad_code_it_was():
    """Distinguishing expired from never-valid tells someone trying codes
    which of their guesses were once real."""
    from app.timeutil import utcnow

    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        record = db.query(BotLinkCode).filter(BotLinkCode.code == code).one()
        record.used_at = utcnow()
        db.commit()

        used = FakeMessage(chat_id=2422)
        asyncio.run(bot.handle_start(used, FakeCommand(args=code), db))

        never = FakeMessage(chat_id=2423)
        asyncio.run(bot.handle_start(never, FakeCommand(args="deadbeef"), db))
    finally:
        db.close()

    assert used.all_text == never.all_text


# --- a truncated list must not read as a complete one ---------------------


def test_channels_says_how_many_it_left_out():
    """Automatic dialog discovery turned the thirty-row cap from theoretical
    into routine: an account with hundreds of dialogs saw thirty of them
    under a heading that read as the whole list."""
    from app.bot.routers.status import CHANNEL_LIMIT

    workspace_id = _linked(2430)
    hidden = 7
    db = SessionLocal()
    try:
        # Alphabetic ids on purpose. Numbering them "disc-0"… put a literal
        # "7" in the listed names, so an assertion for the count passed
        # against the truncating version this test exists to catch.
        for i in range(CHANNEL_LIMIT + hidden):
            get_or_create_channel(
                db, workspace_id=workspace_id, tg_channel_id=f"disc-{chr(97 + i // 26)}{chr(97 + i % 26)}"
            )
        db.commit()
    finally:
        db.close()

    message = FakeMessage(chat_id=2430)
    _run(bot.handle_channels, message)

    listed = message.all_text.count("• disc-")
    assert listed == CHANNEL_LIMIT, f"expected the cap to apply, listed {listed}"
    assert f"{hidden} أخرى" in message.all_text, (
        f"the reply hid {hidden} channels without saying so: {message.all_text!r}"
    )
