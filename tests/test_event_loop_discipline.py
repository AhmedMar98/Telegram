"""Guard the property that a load measurement had to discover the hard way.

A blocking call inside an ``async def`` endpoint does not slow that
endpoint. It stops the whole process, because the event loop is a single
thread and it is also the only thread that can send a response.

``POST /auth/login`` was that endpoint. The measurements, the failed first
fix, and the ``py-spy`` capture of the loop blocked on login's own first
line are written up once, next to the code they govern:
``app.routers.auth.login`` and ``docs/39-concurrency-measurements.md``.
They are deliberately not restated here.

What matters for this file: none of it is visible in a normal test run.
Every other assertion in this suite passed with the defect in place,
because a single request is never slow enough to notice.
"""

from __future__ import annotations

import inspect
import threading

import pytest
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.database import get_db
from app.main import app
from tests.conftest import register_workspace

# Endpoints allowed to be coroutines, keyed by module-qualified name so a
# future endpoint that happens to reuse one of these function names is not
# exempted by accident. Each value is the reason, and the reason is
# surfaced in the failure messages below — a justification nothing ever
# reads is a comment, not a rule.
#
# Every one of these still runs its *database* work on the event loop.
# That is a known limitation, and the honest statement of why it is
# tolerated is narrower than it first appears:
#
#   - None of them runs bcrypt. That is the load-bearing half. login's
#     269ms of key stretching is what turned a latency problem into a
#     health-check failure, and nothing here is remotely that expensive.
#   - Four of the five require an authenticated caller. The fifth,
#     telegram_webhook, does not — it is gated by a shared secret in the
#     path, not by a session — so "authenticated" is not a property of
#     this list and must not be cited as one.
#
# What is *not* a valid reason, though it was originally written here as
# one: "awaits network I/O a worker thread could not perform". A worker
# thread reaches the loop through anyio.from_thread.run, which is exactly
# what login does for its new-device alert. Two entries below
# (recategorize_link, report_workflow_run) are coroutines only to await an
# alert, and could take login's route. They were left alone because
# converting them is a behaviour change outside the measurement that
# justified this file — not because they cannot be converted.
ASYNC_BY_DESIGN = {
    "app.routers.bot_router.telegram_webhook": "aiogram's Dispatcher.feed_update is a coroutine",
    "app.routers.notifications.test_webhook": "delivers an HTTP request to the user's webhook",
    "app.routers.links.add_links": "awaits request.body() for the size-limited raw read",
    "app.routers.links.recategorize_link": "awaits the domain-instability alert",
    "app.routers.status.report_workflow_run": "awaits the workflow-failure alert",
}


def _route_handlers() -> list[tuple[str, object]]:
    """Every endpoint function in the app, keyed by module-qualified name.

    The recursive walk over ``original_router`` — FastAPI hides an
    ``include_router``'s sub-routes there, so reading ``app.routes`` alone
    finds only the handlers defined in ``app/main.py`` — lives in
    ``scripts/api_examples.py`` and is imported rather than copied. It was
    already duplicated once; a third copy would put the one piece of
    FastAPI-internals knowledge that breaks on upgrade in three places.
    """
    from scripts.api_examples import _routes

    return [(f"{route.endpoint.__module__}.{route.name}", route.endpoint) for route in _routes()]


def test_no_endpoint_becomes_a_coroutine_without_saying_why() -> None:
    """An ``async def`` endpoint is a decision, so it has to be declared.

    FastAPI runs a plain ``def`` endpoint in a worker thread, where
    blocking is exactly what threads are for. Writing ``async def`` opts
    out of that protection for every line in the function — including the
    database calls, which look harmless right up until the connection pool
    is empty.

    This does not forbid coroutines. It forbids *silent* ones.
    """
    unexplained = sorted(
        name
        for name, endpoint in _route_handlers()
        if inspect.iscoroutinefunction(endpoint) and name not in ASYNC_BY_DESIGN
    )
    assert unexplained == [], (
        f"these endpoints are 'async def' but not listed in ASYNC_BY_DESIGN: {unexplained}. "
        "Either make them plain 'def' so FastAPI runs them off the event loop, "
        "or add them to the list with the reason they must await. Reasons already "
        f"accepted, for comparison: {ASYNC_BY_DESIGN}"
    )


def test_login_runs_off_the_event_loop(client, monkeypatch) -> None:
    """The password check must happen in a worker thread, not on the loop.

    Asserted through the thread the check actually runs on rather than
    through timing, because a timing assertion at this scale is a flaky
    test: one login is fast enough either way. The thread name is the
    thing that differs, and it differs for exactly the reason that
    matters.
    """
    register_workspace(client, email="loop@example.com", workspace_name="Loop")

    import app.routers.auth as auth_module

    seen: list[str] = []
    real_verify = auth_module.verify_password

    def recording_verify(raw: str, hashed: str) -> bool:
        seen.append(threading.current_thread().name)
        return real_verify(raw, hashed)

    monkeypatch.setattr(auth_module, "verify_password", recording_verify)
    response = client.post(
        "/auth/login",
        json={"email": "loop@example.com", "password": "j8Kd0-slwQ2x"},
    )

    assert response.status_code == 200
    assert seen, "verify_password was never reached — the test no longer exercises login"
    assert all(name.startswith("AnyIO worker thread") for name in seen), (
        f"bcrypt ran on {seen!r}. FastAPI names its threadpool threads "
        "'AnyIO worker thread'; any other name means the handler is a coroutine "
        "again and 269ms of bcrypt is back on the event loop."
    )


def test_an_exhausted_connection_pool_answers_503_not_500(client) -> None:
    """ "Come back shortly" and "I am broken" are different answers.

    SQLAlchemy raises ``TimeoutError`` when every pooled connection is
    checked out and the wait ran past ``pool_timeout``. Unhandled it
    surfaces as a 500, which tells a client never to retry and tells a
    monitor to go hunting for a defect — both wrong, since the same
    request usually succeeds a moment later.
    """

    def exhausted():
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached")

    app.dependency_overrides[get_db] = exhausted
    try:
        response = client.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.headers["X-Error-Code"] == "server_busy"


@pytest.mark.parametrize("name", sorted(ASYNC_BY_DESIGN))
def test_the_allow_list_describes_endpoints_that_exist(name: str) -> None:
    """A stale allow-list entry silently re-permits a name it no longer covers."""
    assert name in {n for n, _ in _route_handlers()}, (
        f"ASYNC_BY_DESIGN lists {name!r} (reason: {ASYNC_BY_DESIGN[name]!r}), which is "
        "no longer a route. Remove it, or the next endpoint given that name is "
        "exempted by accident."
    )


# --- Webhook registration must never fail silently ------------------------
#
# Kept in this file because it is the same defect class the module already
# guards: something the application silently does not do, which no other
# assertion in the suite can see. A service with BOT_TOKEN set and
# PUBLIC_BASE_URL blank starts healthy, serves the dashboard, hands out bot
# link codes — and answers no Telegram message at all, with nothing in the
# log to say why.


def _lifespan_bot_log(caplog, monkeypatch, **env) -> list[str]:
    """Run the app's lifespan with a given bot config, return its log lines."""
    import logging

    import anyio

    from app.config import get_settings
    from app.main import app, lifespan

    for name in ("BOT_TOKEN", "BOT_WEBHOOK_SECRET", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with caplog.at_level(logging.INFO, logger="app.main"):

        async def run() -> None:
            async with lifespan(app):
                pass

        anyio.run(run)

    get_settings.cache_clear()
    return [r.getMessage() for r in caplog.records]


def test_a_bot_token_without_a_public_url_says_so_loudly(caplog, monkeypatch) -> None:
    """The exact configuration that cost a real deployment an evening."""
    messages = _lifespan_bot_log(caplog, monkeypatch, BOT_TOKEN="123:fake", BOT_WEBHOOK_SECRET="s3cr3t")

    warnings = [m for m in messages if "telegram bot NOT active" in m]
    assert warnings, (
        "BOT_TOKEN was set and PUBLIC_BASE_URL was not, so no webhook could be "
        "registered and the bot will answer nothing. That must produce a log line: "
        f"got {messages!r}"
    )
    assert "PUBLIC_BASE_URL" in warnings[0], (
        f"the warning must name the missing variable, not just complain: {warnings[0]!r}"
    )


def test_no_bot_token_stays_quiet(caplog, monkeypatch) -> None:
    """Running without a bot is a normal, chosen configuration, not a fault."""
    messages = _lifespan_bot_log(caplog, monkeypatch)
    assert not [m for m in messages if "telegram bot NOT active" in m], (
        "an operator who set no BOT_TOKEN asked for no bot; warning at them trains "
        "everyone to ignore the warning that matters"
    )


# --- The link code must be spendable without typing it --------------------


def test_the_link_code_response_carries_a_deep_link_when_the_username_is_known(client, monkeypatch) -> None:
    """One tap beats a hand-typed command, and this is why.

    The typed form is "/start <8 hex chars>". It needs the space, it needs
    the leading slash, and the panel that shows it renders right-to-left,
    so the slash appears on the far side and reads as a trailing
    character. A real deployment sent eight variants of that line — the
    first of them correct — and linked nothing.
    """
    import app.bot.telegram_bot as bot_module

    register_workspace(client, email="deeplink@example.com", workspace_name="Deep")
    monkeypatch.setattr(bot_module, "_bot_username", "my_test_bot")

    payload = client.post("/bot/link-code").json()

    assert payload["deep_link"] == f"https://t.me/my_test_bot?start={payload['code']}"
    assert payload["code"] in payload["instructions"], (
        "the typed instruction must describe the same code as the link — two codes "
        "would mean the fallback spends a different one"
    )


def test_an_unknown_username_falls_back_instead_of_emitting_a_broken_link(client, monkeypatch) -> None:
    """Startup may never reach Telegram; the panel must still be usable."""
    import app.bot.telegram_bot as bot_module

    register_workspace(client, email="nolink@example.com", workspace_name="NoLink")
    monkeypatch.setattr(bot_module, "_bot_username", None)

    payload = client.post("/bot/link-code").json()

    assert payload["deep_link"] is None, "a link built without a username would point at https://t.me/None"
    assert payload["code"] in payload["instructions"]
