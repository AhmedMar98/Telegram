"""What Telegram knows about our webhook, as data.

Split out of ``scripts/check_bot.py`` when the same answer was needed from
the dashboard. The reason is not tidiness: Render's free plan gives no
Shell, so on the deployment this project actually targets, a script is a
diagnostic the operator **cannot run**. The endpoint in
``app/routers/bot_router.py`` is the only way to ask this question from a
free-tier deploy, and it must ask it exactly the way the script does or
the two will disagree on the day it matters.

Deliberately synchronous ``urllib``: the caller is a plain ``def``
endpoint that FastAPI runs in a worker thread, where blocking is what
threads are for (see ``app.routers.auth.login`` for why that matters
here). Adding an async HTTP client for one call would buy nothing and put
the call back on the event loop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.telegram.org"
TIMEOUT_SECONDS = 15


def call(token: str, method: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """One Bot API call. Returns the parsed body even on an HTTP error.

    Telegram answers 4xx with a JSON body carrying ``description``, which
    is the useful part — raising on status would throw the explanation
    away and leave the caller with a status code that says nothing.
    """
    url = f"{API}/bot{token}/{method}"
    data = None
    if params:
        data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed https host, not user input
            urllib.request.Request(url, data=data), timeout=TIMEOUT_SECONDS
        ) as response:
            result: dict[str, Any] = json.loads(response.read())
            return result
    except urllib.error.HTTPError as exc:
        try:
            body: dict[str, Any] = json.loads(exc.read())
            return body
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - a network failure is a result, not a crash
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


def diagnose(token: str | None, base_url: str | None, secret: str | None) -> dict[str, Any]:
    """The full picture, with the token never appearing in the result.

    ``verdict`` is the single field a caller should branch on; everything
    else is evidence for a human reading it.
    """
    if not token:
        return {"verdict": "no_token", "detail": "BOT_TOKEN is not set — there is no bot to ask about"}

    me = call(token, "getMe")
    if not me.get("ok"):
        return {
            "verdict": "token_rejected",
            "detail": me.get("description", "unknown"),
            "hint": "A revoked or mistyped token fails here. Get a fresh one from @BotFather.",
        }

    info = call(token, "getWebhookInfo")
    if not info.get("ok"):
        return {"verdict": "api_error", "detail": info.get("description", "unknown")}

    hook = info.get("result", {})
    registered = hook.get("url") or ""
    expected = f"{base_url.rstrip('/')}/telegram/webhook/{secret}" if (base_url and secret) else ""

    # The secret is this endpoint's only authentication; it must not travel
    # in a diagnostic payload, and a URL is not useful to a reader anyway
    # once its path is masked.
    def mask(url: str) -> str:
        return url.replace(secret, "***") if secret and secret in url else url

    if not registered:
        verdict = "no_webhook"
    elif expected and registered != expected:
        verdict = "wrong_url"
    elif hook.get("last_error_message"):
        verdict = "delivery_failing"
    else:
        verdict = "healthy"

    return {
        "verdict": verdict,
        "bot_username": me["result"].get("username"),
        "registered_url": mask(registered),
        "expected_url": mask(expected),
        "pending_update_count": hook.get("pending_update_count", 0),
        "last_error_message": hook.get("last_error_message"),
        "last_error_date": hook.get("last_error_date"),
    }
