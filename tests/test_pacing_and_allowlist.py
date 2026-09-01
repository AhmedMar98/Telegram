"""Two defences added on the same day, for the same reason: the system was
reactive where it needed to be proactive.

The pacer's tests are mostly about its *budget*, not its delays. A shield
that pauses generously enough to time out an hourly job causes the outage
it was added to prevent, and a timed-out collector is indistinguishable
from a broken one.
"""

from __future__ import annotations

import asyncio

import pytest

import scripts.collect as collector
from app.bot import telegram_bot as bot

# --- the pacer -------------------------------------------------------------


def _recording_pacer(*, minimum=1.0, maximum=3.0, budget=10.0, draw=None):
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return (
        collector.Pacer(
            minimum=minimum,
            maximum=maximum,
            budget=budget,
            sleep=sleep,
            jitter=draw or (lambda lo, hi: hi),
        ),
        slept,
    )


def test_pauses_are_drawn_from_the_range_not_fixed():
    """A constant gap between requests is itself a signature. The thing
    being avoided is looking like a program."""
    draws = iter([1.0, 2.5, 1.7])
    pacer, slept = _recording_pacer(draw=lambda lo, hi: next(draws))

    for _ in range(3):
        asyncio.run(pacer.wait())

    assert slept == [1.0, 2.5, 1.7]


def test_the_budget_is_a_ceiling_the_pacer_cannot_exceed():
    """The failure this prevents: 4s x 20 dialogs x 10 accounts is 800
    seconds added to an hourly job, on runners already late under load."""
    pacer, slept = _recording_pacer(minimum=3.0, maximum=3.0, budget=10.0)

    for _ in range(20):
        asyncio.run(pacer.wait())

    assert sum(slept) <= 10.0, f"pacing spent {sum(slept)}s against a 10s budget"
    assert pacer.exhausted


def test_a_spent_budget_stops_pacing_rather_than_stopping_work():
    """Slowing down is a defence; refusing to finish is not. Once the
    budget is gone the collector must keep reading its remaining dialogs."""
    pacer, slept = _recording_pacer(minimum=5.0, maximum=5.0, budget=5.0)

    asyncio.run(pacer.wait())
    before = len(slept)
    for _ in range(10):
        asyncio.run(pacer.wait())

    assert len(slept) == before, "pacing continued past the budget"
    assert pacer.exhausted


def test_the_final_pause_is_clamped_to_what_is_left():
    """Otherwise the budget is a suggestion that one full-length pause can
    overshoot."""
    pacer, slept = _recording_pacer(minimum=4.0, maximum=4.0, budget=6.0)

    asyncio.run(pacer.wait())
    asyncio.run(pacer.wait())

    assert sum(slept) == pytest.approx(6.0)


def test_pacing_configured_to_zero_never_sleeps():
    """An operator who turns it off must get it off, not a zero-length
    await per dialog."""
    pacer, slept = _recording_pacer(minimum=0.0, maximum=0.0, budget=100.0)

    asyncio.run(pacer.wait())

    assert slept == []


def test_the_collector_paces_between_dialogs_and_not_before_the_first():
    """The pause belongs *between* reads. Pausing before the only read in a
    run is pure latency hiding nothing."""
    import inspect

    source = inspect.getsource(collector._collect_with_account)
    assert "if index:" in source and "await pacer.wait()" in source, (
        "the pacer is no longer wired into the per-dialog loop"
    )


def test_one_pacer_covers_the_whole_run_not_one_per_account():
    """Ten accounts with their own 240-second budget is 2,400 seconds —
    which is the timeout the budget exists to avoid."""
    import inspect

    source = inspect.getsource(collector.collect)
    assert source.count("_pacer()") == 1, "the budget must be shared across accounts"


# --- the bot allowlist -----------------------------------------------------


class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id


class _Message:
    def __init__(self, chat_id):
        self.chat = _Chat(chat_id)


class _Update:
    def __init__(self, chat_id=None):
        self.message = _Message(chat_id) if chat_id is not None else None
        self.edited_message = None
        self.callback_query = None


async def _dispatch(update, allowed: str):
    from app.config import get_settings

    get_settings.cache_clear()
    import os

    os.environ["BOT_ALLOWED_CHAT_IDS"] = allowed
    try:
        reached = []

        async def handler(event, data):
            reached.append(event)
            return "handled"

        result = await bot.AllowlistMiddleware()(handler, update, {})
        return result, reached
    finally:
        os.environ.pop("BOT_ALLOWED_CHAT_IDS", None)
        get_settings.cache_clear()


def test_an_empty_allowlist_lets_everyone_through():
    """Every existing deployment runs with this unset. Turning the feature
    on by default would lock out every linked chat on upgrade."""
    result, reached = asyncio.run(_dispatch(_Update(555), allowed=""))

    assert result == "handled" and len(reached) == 1


def test_a_listed_chat_is_allowed_through():
    result, reached = asyncio.run(_dispatch(_Update(555), allowed="555,777"))

    assert result == "handled" and len(reached) == 1


def test_an_unlisted_chat_is_dropped_silently():
    """Silent on purpose. "You are not authorised" confirms the bot exists
    and is guarded, which a stranger has no reason to learn."""
    result, reached = asyncio.run(_dispatch(_Update(999), allowed="555"))

    assert result is None and reached == []


def test_an_update_with_no_identifiable_chat_fails_closed():
    """With an allowlist configured, "I could not tell whose this is" must
    be a refusal — otherwise the allowlist is advisory."""
    result, reached = asyncio.run(_dispatch(_Update(None), allowed="555"))

    assert result is None and reached == []


def test_the_allowlist_runs_before_a_database_session_is_opened():
    """A rejected update must not cost a connection to reject."""
    import inspect

    source = inspect.getsource(bot)
    allow = source.index("dispatcher.update.middleware(AllowlistMiddleware())")
    session = source.index("dispatcher.update.middleware(DbSessionMiddleware())")
    assert allow < session, "the allowlist must be registered before the session middleware"
