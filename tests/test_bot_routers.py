"""The one thing splitting the bot into routers can silently break.

Handlers moving between files is mechanical and provable: the suite in
tests/test_bot_handlers.py is byte-identical across the split and still
passes, which is what makes the reorganisation verifiably
behaviour-preserving.

**Registration order is the exception.** aiogram offers each update to
routers in the order they were included, and the search router ends with
a catch-all ``F.text`` handler. Include it first and it swallows every
command behind it — ``/stats`` would be answered as a *search for the
string "/stats"*. Nothing about that failure looks like a crash: the bot
replies, the tests that call handlers directly all pass, and only a real
person typing a command notices.

So the order is pinned as data here, not trusted to a comment in the
composition root.
"""

from __future__ import annotations

from app.bot import telegram_bot as bot
from app.bot.shared import COMMANDS

# The catch-all must be last. Stated as a name rather than an index so the
# assertion reads as the rule it enforces.
CATCH_ALL_ROUTER = "search"


def _router_names() -> list[str]:
    return [router.name for router in bot.dispatcher.sub_routers]


def test_the_dispatcher_actually_has_the_routers():
    """Guards the guard: an empty list would make every assertion below
    vacuously true, which is how a check like this stops checking."""
    names = _router_names()

    assert len(names) == 4, f"expected four routers, found {names}"
    assert set(names) == {"onboarding", "status", "onboard_account", "search"}


def test_the_catch_all_router_is_registered_last():
    """The failure this exists for: a catch-all registered before the
    command routers answers /stats as a search for the word "/stats"."""
    names = _router_names()

    assert names[-1] == CATCH_ALL_ROUTER, (
        f"router order is {names}. The router carrying the catch-all F.text handler "
        f"({CATCH_ALL_ROUTER!r}) must be last, or it shadows every command behind it."
    )


def test_every_documented_command_is_reachable_through_some_router():
    """Guards against a command that survived the split in name only.

    tests/test_bot_handlers.py already checks COMMANDS against a hand-written
    map of handlers. This checks the other half: that each of those handlers
    is actually *registered on a router the dispatcher knows about*, which
    is what a move between files can break without touching a single
    handler body.
    """
    registered = {
        handler.callback.__name__ for router in bot.dispatcher.sub_routers for handler in router.message.handlers
    }

    expected = {
        "/search": "handle_search",
        "/latest": "handle_latest",
        "/favorite": "handle_favorite",
        "/details": "handle_details",
        "/stats": "handle_stats",
        "/vitality": "handle_vitality",
        "/channels": "handle_channels",
        "/unlink": "handle_unlink",
        "/help": "handle_help",
    }
    assert {name for name, _ in COMMANDS} == set(expected), "COMMANDS changed; update this map deliberately"

    missing = sorted(func for func in expected.values() if func not in registered)
    assert not missing, f"documented commands whose handler is registered nowhere: {missing}"


def test_the_module_still_exposes_its_handlers_by_name():
    """The composition root is the public surface.

    Callers — the test suite included — address handlers as attributes of
    app.bot.telegram_bot. Moving a function into a router package must not
    become a rename for everyone importing it.
    """
    for name in (
        "handle_start",
        "handle_help",
        "handle_unlink",
        "handle_search",
        "handle_latest",
        "handle_favorite",
        "handle_details",
        "handle_page",
        "handle_other",
        "handle_stats",
        "handle_channels",
        "handle_vitality",
    ):
        assert callable(getattr(bot, name, None)), f"{name} is no longer reachable on app.bot.telegram_bot"


def test_the_account_flow_router_comes_before_the_catch_all():
    """Both register an F.text handler, and the ordering decides which one
    sees a phone number typed mid-flow.

    Registered the other way round, /addaccount would ask for a phone
    number and the search router would answer it with "no results" — a
    failure that looks like a broken search rather than a broken ordering.
    """
    names = _router_names()

    assert names.index("onboard_account") < names.index("search")
