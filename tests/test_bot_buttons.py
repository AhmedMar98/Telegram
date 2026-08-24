"""The bot must be operable without memorising anything.

Ten commands existed and two buttons: previous and next. Everything else
had to be typed from memory, including `/details 42`, where the 42 is only
readable while the results are still on screen.

That is not an ergonomics footnote. The linking flow asked for
`/start <code>` — a slash, a space, eight hex characters — rendered
right-to-left so the slash appears on the far side of the line. A real user
sent eight variants of it and linked nothing. Typing is the failure mode.

These tests cover the buttons that replaced it, and one property that is
security rather than convenience: callback payloads are client-supplied, so
a link id in a button must not resolve outside the sending chat's
workspace.
"""

from __future__ import annotations

import asyncio

from app.bot import telegram_bot as bot
from app.bot.shared import CB_DETAILS, CB_FAVOURITE, CB_MENU
from app.database import SessionLocal
from app.models import Link
from tests.test_bot_handlers import FakeMessage, _linked, _seed, _workspace


class FakeCallback:
    """A tapped button. Records the toast and any keyboard repaint."""

    def __init__(self, data: str, message: FakeMessage | None):
        self.data = data
        self.message = message
        self.answers: list[tuple[str, bool]] = []
        self.edited: list[object] = []
        if message is not None:
            message.edit_reply_markup = self._edit  # type: ignore[attr-defined]

    async def _edit(self, reply_markup: object = None) -> None:
        self.edited.append(reply_markup)

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _run(handler, callback) -> None:
    db = SessionLocal()
    try:
        asyncio.run(handler(callback, db))
    finally:
        db.close()


def _first_link_id(workspace_id: int) -> int:
    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.workspace_id == workspace_id).first()
        assert link is not None
        return int(link.id)
    finally:
        db.close()


def test_a_result_carries_its_own_details_and_star_buttons() -> None:
    workspace_id = _linked(3001)
    _seed(workspace_id, ["https://example.com/paper.pdf"])

    message = FakeMessage(chat_id=3001)
    db = SessionLocal()
    try:
        asyncio.run(bot.handle_latest(message, db))
    finally:
        db.close()

    labels = [b.text for m in message.keyboards for row in m.inline_keyboard for b in row]
    assert any("تفاصيل" in t for t in labels), f"no details button on a result: {labels}"
    assert any("مفضّلة" in t for t in labels), f"no favourite button on a result: {labels}"


def test_the_details_button_answers_without_anyone_typing_an_id() -> None:
    workspace_id = _linked(3002)
    _seed(workspace_id, ["https://example.com/networks.pdf"])
    link_id = _first_link_id(workspace_id)

    message = FakeMessage(chat_id=3002)
    callback = FakeCallback(f"{CB_DETAILS}:{link_id}", message)
    _run(bot.handle_details_button, callback)

    assert "networks.pdf" in message.all_text
    assert callback.answers, "the button must be acknowledged or it spins forever"


def test_the_star_button_records_the_wanted_state_not_a_toggle() -> None:
    """Two chats sending "toggle" would cancel each other out."""
    workspace_id = _linked(3003)
    _seed(workspace_id, ["https://example.com/starred.pdf"])
    link_id = _first_link_id(workspace_id)

    message = FakeMessage(chat_id=3003)
    _run(bot.handle_favourite_button, FakeCallback(f"{CB_FAVOURITE}:{link_id}:1", message))

    db = SessionLocal()
    try:
        assert db.get(Link, link_id).is_favorite is True
    finally:
        db.close()

    # Sending the same intent again must be idempotent, not a flip.
    _run(bot.handle_favourite_button, FakeCallback(f"{CB_FAVOURITE}:{link_id}:1", FakeMessage(chat_id=3003)))
    db = SessionLocal()
    try:
        assert db.get(Link, link_id).is_favorite is True, "the same intent twice must not undo itself"
    finally:
        db.close()


def test_the_star_button_repaints_itself_to_match_the_new_state() -> None:
    """A label describing the state before the tap is a lying button."""
    workspace_id = _linked(3004)
    _seed(workspace_id, ["https://example.com/repaint.pdf"])
    link_id = _first_link_id(workspace_id)

    message = FakeMessage(chat_id=3004)
    callback = FakeCallback(f"{CB_FAVOURITE}:{link_id}:1", message)
    _run(bot.handle_favourite_button, callback)

    assert callback.edited, "the keyboard must be redrawn after the state changes"
    labels = [b.text for row in callback.edited[0].inline_keyboard for b in row]
    assert any("إزالة" in t for t in labels), f"the star should now offer removal: {labels}"


def test_a_button_cannot_reach_a_link_in_another_workspace() -> None:
    """Callback data is client-supplied. This is isolation, not ergonomics."""
    mine = _linked(3005)
    _seed(mine, ["https://example.com/mine.pdf"])
    theirs = _workspace()
    _seed(theirs, ["https://example.com/theirs.pdf"])
    their_link_id = _first_link_id(theirs)

    message = FakeMessage(chat_id=3005)
    callback = FakeCallback(f"{CB_DETAILS}:{their_link_id}", message)
    _run(bot.handle_details_button, callback)

    assert "theirs.pdf" not in message.all_text, "a foreign link leaked through a button"
    assert callback.answers and callback.answers[0][1] is True, "refusal should be an alert"

    # The same must hold for a write, not only a read.
    before = FakeMessage(chat_id=3005)
    _run(bot.handle_favourite_button, FakeCallback(f"{CB_FAVOURITE}:{their_link_id}:1", before))
    db = SessionLocal()
    try:
        assert db.get(Link, their_link_id).is_favorite is not True, "a foreign link was modified"
    finally:
        db.close()


def test_the_menu_buttons_reach_the_same_answers_as_the_commands() -> None:
    workspace_id = _linked(3006)
    _seed(workspace_id, ["https://example.com/menu.pdf"])

    message = FakeMessage(chat_id=3006)
    _run(bot.handle_menu_button, FakeCallback(f"{CB_MENU}:stats", message))
    assert "عدد الروابط" in message.all_text

    latest = FakeMessage(chat_id=3006)
    _run(bot.handle_menu_button, FakeCallback(f"{CB_MENU}:latest", latest))
    assert "menu.pdf" in latest.all_text


def test_an_unlinked_chat_is_refused_by_every_button() -> None:
    for handler, data in (
        (bot.handle_details_button, f"{CB_DETAILS}:1"),
        (bot.handle_favourite_button, f"{CB_FAVOURITE}:1:1"),
        (bot.handle_menu_button, f"{CB_MENU}:stats"),
    ):
        message = FakeMessage(chat_id=999999)
        callback = FakeCallback(data, message)
        _run(handler, callback)
        assert callback.answers and callback.answers[0][1] is True, (
            f"{handler.__name__} let an unlinked chat through"
        )


def test_a_message_too_old_to_read_back_is_explained_not_ignored() -> None:
    """Telegram sends InaccessibleMessage; the button must not just spin."""
    callback = FakeCallback(f"{CB_MENU}:stats", None)
    _run(bot.handle_menu_button, callback)

    assert callback.answers and callback.answers[0][1] is True
    assert "أقدم" in callback.answers[0][0]
