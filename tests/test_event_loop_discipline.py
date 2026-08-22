"""Guard the property that a load measurement had to discover the hard way.

A blocking call inside an ``async def`` endpoint does not slow that
endpoint. It stops the whole process, because the event loop is a single
thread and it is also the only thread that can send a response.

``POST /auth/login`` was that endpoint, and the damage was measured rather
than argued (docs/39-concurrency-measurements.md):

  - 20 concurrent logins took 5,687ms — twenty times 285ms, in single
    file, with no two overlapping.
  - ``GET /healthz``, which touches nothing, went from 7ms to 5,653ms.
    ``render.yaml`` names that path as ``healthCheckPath``.
  - Under connection-pool pressure ``py-spy`` caught the event loop
    blocked on login's *first* line, ``is_locked_out``, waiting inside
    SQLAlchemy's pool while all fourteen worker threads sat idle.

Nothing about that is visible in a normal test run: every assertion in
this suite passes with the defect in place, because a single request is
never slow enough to notice. These two tests exist so the property is
checked rather than remembered.
"""

from __future__ import annotations

import inspect
import threading

import pytest
from fastapi.routing import APIRoute
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.database import get_db
from app.main import app
from tests.conftest import register_workspace

# Endpoints allowed to be coroutines, each because it genuinely awaits I/O
# that has no synchronous form. Anything else added here needs the same
# justification in writing — that is the point of the list.
#
# Every one of these still runs its *database* work on the event loop, and
# that is a known, accepted limitation rather than an oversight: all four
# require an authenticated caller, none runs bcrypt, and each awaits real
# network I/O that a worker thread could not perform. login was different
# on all three counts, which is why it is not here.
ASYNC_BY_DESIGN = {
    "telegram_webhook": "aiogram's Dispatcher.feed_update is a coroutine",
    "test_webhook": "delivers an HTTP request to the user's webhook",
    "add_links": "awaits request.body() for the size-limited raw read",
    "recategorize_link": "awaits the domain-instability alert",
    "report_workflow_run": "awaits the workflow-failure alert",
}


def _route_handlers() -> list[tuple[str, object]]:
    """Every endpoint function in the app, including the ones behind routers.

    Walked recursively rather than read off ``app.routes``, because that
    list is not flat: FastAPI wraps ``include_router`` results in an
    internal holder object whose sub-routes live on ``original_router``.
    Reading only the top level finds the six endpoints defined in
    ``app/main.py`` and silently misses every router — which is to say it
    would miss ``login``, the endpoint this whole module exists for.
    """
    found: list[tuple[str, object]] = []
    seen: set[int] = set()

    def walk(routes) -> None:
        for route in routes:
            if id(route) in seen:
                continue
            seen.add(id(route))
            if isinstance(route, APIRoute):
                found.append((route.name, route.endpoint))
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)

    walk(app.routes)
    return found


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
        "or add them to the list with the reason they must await."
    )


def test_login_runs_off_the_event_loop(client) -> None:
    """The password check must happen in a worker thread, not on the loop.

    Asserted through the thread the check actually runs on rather than
    through timing, because a timing assertion at this scale is a flaky
    test: one login is fast enough either way. The thread name is the
    thing that differs, and it differs for exactly the reason that
    matters.
    """
    register_workspace(client, email="loop@example.com", workspace_name="Loop")

    seen: list[str] = []
    import app.routers.auth as auth_module

    real_verify = auth_module.verify_password

    def recording_verify(raw: str, hashed: str) -> bool:
        seen.append(threading.current_thread().name)
        return real_verify(raw, hashed)

    auth_module.verify_password = recording_verify  # type: ignore[assignment]
    try:
        response = client.post(
            "/auth/login",
            json={"email": "loop@example.com", "password": "j8Kd0-slwQ2x"},
        )
    finally:
        auth_module.verify_password = real_verify  # type: ignore[assignment]

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
        f"ASYNC_BY_DESIGN lists {name!r}, which is no longer a route. "
        "Remove it, or the next endpoint given that name is exempted by accident."
    )
