"""Which credential each endpoint accepts, enforced as a list.

``app/deps.py`` splits authentication into two dependencies so that "an
API key must not reach this" is expressed in a signature rather than
remembered. That works for the endpoints written so far. This file closes
the remaining gap: an endpoint added *later* with the wrong dependency
would compile, pass its own tests, and silently widen what a leaked key
can do.

So the split is pinned as data. Adding or moving an endpoint fails here
until someone states which side it belongs on — the same "guards the
guard" shape as ``tests/test_isolation_sweep.py``.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.deps import get_current_user, get_session_user
from app.main import app

# Session cookie only. Everything here either destroys something
# irreversibly, manages credentials, or exposes the owner's own devices.
SESSION_ONLY = {
    ("POST", "/auth/logout"),
    ("POST", "/auth/logout-all"),
    ("POST", "/auth/change-password"),
    ("POST", "/auth/me/delete"),
    # The security log is narrower than the data export but more sensitive
    # per row: it is the account's own IP addresses, devices and sign-in
    # history. No script needs it, and a leaked key must not be able to
    # profile the owner's movements.
    ("GET", "/auth/me/security-export"),
    ("GET", "/auth/sessions"),
    ("DELETE", "/auth/sessions/{session_id}"),
    ("GET", "/auth/security-activity"),
    ("GET", "/auth/api-keys"),
    # The second factor is a credential too: an API key must not be able to
    # attach one to an account (a way to lock the owner out), read whether
    # one is set, or strip one off.
    ("GET", "/auth/totp"),
    ("POST", "/auth/totp/setup"),
    ("POST", "/auth/totp/enable"),
    ("POST", "/auth/totp/recovery-codes"),
    ("POST", "/auth/totp/disable"),
    # Notification settings are a security surface in their own right: a
    # leaked key that could switch alerts off would be a way to silence
    # the very warnings that report the leak.
    ("GET", "/notifications"),
    ("POST", "/notifications/{notification_id:int}/read"),
    ("POST", "/notifications/read-all"),
    ("GET", "/notifications/preferences"),
    ("PATCH", "/notifications/preferences/{alert_type}"),
    ("GET", "/notifications/unread-count"),
    # The outbound webhook (idea 162). Session-only for a reason worth
    # stating: an API key that could repoint it would turn a leaked key
    # into a way to redirect a workspace's alerts to an address the
    # attacker controls — which is a great deal more than the noise a
    # leaked key is supposed to be capable of.
    ("GET", "/notifications/webhook"),
    ("PUT", "/notifications/webhook"),
    ("DELETE", "/notifications/webhook"),
    ("POST", "/notifications/webhook/test"),
    # The status screen exposes deploy identity, live counters and the
    # workflow board. Reporting *into* it takes an API key on purpose
    # (that is the credential inversion); reading it does not.
    ("GET", "/status"),
    ("POST", "/auth/api-keys"),
    ("DELETE", "/auth/api-keys/{key_id:int}"),
    # Adding a collecting account is a real Telegram login: this mints a
    # new bearer credential for a third-party account. An API key must
    # not be able to trigger that any more than it can change the
    # workspace's own password.
    ("POST", "/channels/accounts/login/start"),
    ("POST", "/channels/accounts/login/verify"),
}


def _routes() -> list[APIRoute]:
    def walk(router) -> list[APIRoute]:
        found: list[APIRoute] = []
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                found.append(route)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                found.extend(walk(inner))
        return found

    return walk(app)


def _uses(route: APIRoute, dependency) -> bool:
    """Whether a dependency appears anywhere in a route's dependency tree."""

    def flatten(dep):
        yield dep.call
        for sub in dep.dependencies:
            yield from flatten(sub)

    return dependency in set(flatten(route.dependant))


def _classified() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    session_only, either = set(), set()
    for route in _routes():
        for method in route.methods:
            if _uses(route, get_session_user):
                session_only.add((method, route.path))
            elif _uses(route, get_current_user):
                either.add((method, route.path))
    return session_only, either


def test_the_route_walk_actually_finds_routes():
    """Guards the guard's guard. A walk that silently returns nothing would
    make every assertion below pass while checking absolutely nothing —
    which is exactly how this test was first written."""
    session_only, either = _classified()

    assert len(_routes()) > 30
    assert len(either) > 15
    assert len(session_only) == len(SESSION_ONLY)


def test_session_only_endpoints_are_exactly_the_declared_set():
    """A new destructive or credential endpoint must be listed here on
    purpose. Until it is, this fails rather than shipping an endpoint a
    leaked API key can reach."""
    session_only, _ = _classified()

    assert session_only == SESSION_ONLY, (
        f"Authentication boundary changed. Difference: {sorted(session_only ^ SESSION_ONLY)}"
    )


def test_no_endpoint_accepts_both_dependencies():
    """Declaring both would make the stricter one decorative."""
    session_only, either = _classified()

    assert not (session_only & either)


def test_every_api_key_endpoint_is_session_only():
    """Stated separately from the list above because it is the rule that
    makes revocation mean anything: a key that can issue keys cannot be
    revoked in any useful sense."""
    for method, path in {p for p in _classified()[0] | _classified()[1] if "/auth/api-keys" in p[1]}:
        assert (method, path) in SESSION_ONLY, f"{method} {path} is reachable with an API key"
