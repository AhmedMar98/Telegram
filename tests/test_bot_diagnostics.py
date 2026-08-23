"""The bot's silence must be explainable from the deployment that ships.

``scripts/check_bot.py`` answers the same question and is the better tool
anywhere with a terminal. It is not available on the target: Render's free
plan provides no Shell, so on a free-tier deploy the script cannot be run
at all — which is exactly the deploy where a silent bot happened.

These tests fence the two things that make the endpoint trustworthy: it
must classify each failure distinctly enough to act on, and it must never
put a bearer credential in a response body.
"""

from __future__ import annotations

import pytest

from app import botdiag
from tests.conftest import register_workspace

BASE = "https://example.onrender.com"
SECRET = "webhook-secret-value"


@pytest.fixture
def fake_api(monkeypatch):
    """Replace the Telegram round trip. Nothing here touches the network."""

    def install(responses: dict[str, dict]) -> None:
        def fake_call(token: str, method: str, params=None) -> dict:
            return responses[method]

        monkeypatch.setattr(botdiag, "call", fake_call)

    return install


def _hook(**fields) -> dict:
    return {"ok": True, "result": {"url": "", "pending_update_count": 0, **fields}}


def _me(username: str = "a_bot") -> dict:
    return {"ok": True, "result": {"username": username}}


def test_no_webhook_is_reported_as_its_own_verdict(fake_api) -> None:
    """The case that actually happened: nothing registered, so nothing arrives."""
    fake_api({"getMe": _me(), "getWebhookInfo": _hook(url="")})

    result = botdiag.diagnose("tok", BASE, SECRET)

    assert result["verdict"] == "no_webhook"


def test_a_stale_url_is_not_confused_with_a_healthy_one(fake_api) -> None:
    """Registered-but-wrong looks identical to registered-and-right from outside."""
    fake_api({"getMe": _me(), "getWebhookInfo": _hook(url="https://old-deploy.onrender.com/telegram/webhook/x")})

    result = botdiag.diagnose("tok", BASE, SECRET)

    assert result["verdict"] == "wrong_url"


def test_a_failing_delivery_surfaces_telegrams_own_words(fake_api) -> None:
    """last_error_message is usually the whole answer, and is invisible otherwise."""
    fake_api(
        {
            "getMe": _me(),
            "getWebhookInfo": _hook(
                url=f"{BASE}/telegram/webhook/{SECRET}",
                last_error_message="Read timeout expired",
                pending_update_count=4,
            ),
        }
    )

    result = botdiag.diagnose("tok", BASE, SECRET)

    assert result["verdict"] == "delivery_failing"
    assert result["last_error_message"] == "Read timeout expired"
    assert result["pending_update_count"] == 4


def test_a_revoked_token_is_told_apart_from_a_webhook_problem(fake_api) -> None:
    """After a /revoke these look the same, and the fixes are opposite."""
    fake_api({"getMe": {"ok": False, "description": "Unauthorized"}})

    result = botdiag.diagnose("tok", BASE, SECRET)

    assert result["verdict"] == "token_rejected"


def test_the_payload_never_carries_the_token_or_the_webhook_secret(fake_api) -> None:
    """A diagnostic that leaks the thing it diagnoses is a vulnerability.

    The webhook secret is that endpoint's ONLY authentication: anyone
    holding it can post forged Telegram updates to this deployment.
    """
    fake_api({"getMe": _me(), "getWebhookInfo": _hook(url=f"{BASE}/telegram/webhook/{SECRET}")})

    result = botdiag.diagnose("super-secret-token", BASE, SECRET)

    serialised = repr(result)
    assert "super-secret-token" not in serialised
    assert SECRET not in serialised, "the webhook secret must be masked before it reaches a caller"
    assert "***" in result["registered_url"]


def test_the_endpoint_requires_a_logged_in_user(client) -> None:
    """It reports on deployment configuration, so it is not public."""
    assert client.get("/bot/diagnostics").status_code in (401, 403)


def test_a_logged_in_user_gets_a_verdict(client, monkeypatch) -> None:
    monkeypatch.setattr(botdiag, "call", lambda *a, **k: {"ok": False, "description": "no network"})
    register_workspace(client, email="diag@example.com", workspace_name="Diag")

    payload = client.get("/bot/diagnostics").json()

    assert "verdict" in payload
