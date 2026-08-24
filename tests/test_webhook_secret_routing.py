"""The webhook must survive whatever BOT_WEBHOOK_SECRET happens to contain.

This file exists because of one line from a real deployment's log:

    91.108.5.77 - "POST /telegram/webhook/bLRINbE/hEYeSol/YElTYQ…%3D" 404

91.108.5.77 is Telegram. It was delivering correctly, to a webhook that
was correctly registered, and getting 404 — because the route was
``/telegram/webhook/{secret}`` and a path parameter stops at ``/``. Render
generates BOT_WEBHOOK_SECRET with ``generateValue: true``, which produces
base64: slashes and equals signs are ordinary characters in it.

From the outside this was indistinguishable from a bot that ignores valid
messages. Every gate in this repository was green while it was broken:
no test ever sent a secret that was not a tidy identifier.

The same line shows the second defect. The secret is *in the log*, in
clear text, on every delivery — and it is the only thing standing between
a stranger and posting forged updates into this deployment.
"""

from __future__ import annotations

import pytest

from app.bot.telegram_bot import WEBHOOK_SECRET_HEADER, webhook_token

# The real generated value from the failing deployment, character for
# character. A test that invents a "tricky" secret is a test that guesses;
# this is the shape the platform actually produces.
RENDER_STYLE_SECRET = "bLRINbE/hEYeSol/YElTYQIpGvJsuD0Uhm54NEaHAVQ="


@pytest.mark.parametrize(
    "secret",
    [
        RENDER_STYLE_SECRET,
        "plain-identifier-secret",
        "with+plus/and=equals",
        "ends-with-slash/",
    ],
)
def test_the_derived_token_is_always_legal_for_telegram(secret: str) -> None:
    """Telegram accepts only [A-Za-z0-9_-] in secret_token, 1–256 chars.

    Passing Render's base64 straight through is rejected by the API, which
    is the other half of why the secret cannot simply be forwarded.
    """
    token = webhook_token(secret)

    assert 1 <= len(token) <= 256
    assert all(c in "0123456789abcdef" for c in token), (
        f"{token!r} must be inside Telegram's alphabet for any input secret"
    )
    assert "/" not in token, "a slash here is exactly the bug this file guards"


def test_different_secrets_derive_different_tokens() -> None:
    assert webhook_token("a") != webhook_token("b")


def test_the_route_no_longer_carries_the_secret_in_its_path() -> None:
    """A URL is written to every access log it passes through.

    The path must be constant, so nothing about the credential can be
    recovered from a log line — which is how this deployment's secret
    ended up pasted into a chat.
    """
    from app.main import app
    from scripts.api_examples import _routes

    webhook_paths = [r.path for r in _routes() if "telegram/webhook" in r.path]

    assert webhook_paths == ["/telegram/webhook"], (
        f"expected one constant path, got {webhook_paths}. A path parameter here "
        "both leaks the secret into logs and breaks routing when the secret "
        "contains a separator."
    )
    assert app is not None


def test_a_delivery_carrying_the_right_header_is_accepted(client, monkeypatch) -> None:
    """The whole point: Telegram's real request shape must get through."""
    from app.config import get_settings

    monkeypatch.setenv("BOT_WEBHOOK_SECRET", RENDER_STYLE_SECRET)
    monkeypatch.setenv("BOT_TOKEN", "123:fake")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/telegram/webhook",
            json={"update_id": 1},
            headers={WEBHOOK_SECRET_HEADER: webhook_token(RENDER_STYLE_SECRET)},
        )
        # Anything but 404 means the route matched and the secret verified;
        # what the dispatcher then does with a bare update is not this
        # file's business.
        assert response.status_code != 404, "a correctly-signed delivery was refused — this is the original bug"
    finally:
        get_settings.cache_clear()


def test_a_delivery_with_no_header_or_a_wrong_one_is_refused(client, monkeypatch) -> None:
    """404 rather than 401: an unauthenticated caller learns nothing."""
    from app.config import get_settings

    monkeypatch.setenv("BOT_WEBHOOK_SECRET", RENDER_STYLE_SECRET)
    get_settings.cache_clear()
    try:
        assert client.post("/telegram/webhook", json={"update_id": 1}).status_code == 404
        assert (
            client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={WEBHOOK_SECRET_HEADER: "wrong"},
            ).status_code
            == 404
        )
        # The raw secret is not the token, and must not be accepted as one.
        assert (
            client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={WEBHOOK_SECRET_HEADER: RENDER_STYLE_SECRET},
            ).status_code
            == 404
        )
    finally:
        get_settings.cache_clear()
