"""Ask Telegram why the bot is silent, instead of guessing.

``check_setup.py`` answers "is the configuration present?". This answers
the question that actually matters when a bot ignores a perfectly valid
``/start``: **did Telegram ever accept a webhook, and what happened the
last time it tried to deliver to it?**

Nothing in the repository asked that before, which is why a real
deployment burned an evening on it. The configuration can be flawless and
the bot still deaf, for reasons only Telegram knows:

- no webhook was ever registered (the usual one — PUBLIC_BASE_URL was
  blank at boot, so app/main.py's lifespan skipped registration entirely)
- a webhook is registered but points at a stale URL from an earlier deploy
- Telegram is registered correctly and its deliveries are FAILING, which
  is invisible from our side: getWebhookInfo carries last_error_message,
  and it is usually the whole answer
- updates are queued and undelivered (pending_update_count climbing)

Run it wherever BOT_TOKEN is set — Render Shell is the natural place:

    python scripts/check_bot.py           # report only
    python scripts/check_bot.py --fix     # re-register the webhook too

The token is read from the environment and never printed. The webhook
secret is masked in output: it is that endpoint's only authentication.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import UTC, datetime

# The dashboard endpoint (app/routers/bot_router.py) answers the same
# question through app.botdiag. Sharing the caller is what keeps the two
# from disagreeing on the day one of them is the only one available —
# Render's free plan has no Shell, so on that deploy this script cannot
# run at all and the endpoint is the only route to this answer.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.botdiag import call as _call  # noqa: E402

OK, WARN, FAIL = "[OK]", "[WARN]", "[FAIL]"


def report(status: str, name: str, detail: str) -> None:
    print(f"{status:7} {name:24} {detail}")


def _mask_secret(url: str, secret: str | None) -> str:
    return url.replace(secret, "***") if secret and secret in url else url


def _age(timestamp: int) -> str:
    delta = datetime.now(UTC) - datetime.fromtimestamp(timestamp, UTC)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 1440:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="re-register the webhook from PUBLIC_BASE_URL and BOT_WEBHOOK_SECRET",
    )
    args = parser.parse_args()

    token = os.environ.get("BOT_TOKEN")
    base_url = os.environ.get("PUBLIC_BASE_URL")
    secret = os.environ.get("BOT_WEBHOOK_SECRET")

    if not token:
        report(FAIL, "BOT_TOKEN", "not set — there is no bot to ask about")
        return 1

    # 1. Is the token live? A revoked token looks exactly like a typo'd one
    #    from the outside, and both are common after a /revoke.
    me = _call(token, "getMe")
    if not me.get("ok"):
        report(FAIL, "BOT_TOKEN", f"rejected by Telegram — {me.get('description', 'unknown')}")
        print("\n  A revoked or mistyped token fails here. Get a fresh one from @BotFather")
        print("  with /token, set it in the environment, and redeploy.")
        return 1
    username = me["result"].get("username", "?")
    report(OK, "BOT_TOKEN", f"accepted — bot is @{username}")

    # 2. The actual question.
    info = _call(token, "getWebhookInfo")
    if not info.get("ok"):
        report(FAIL, "getWebhookInfo", info.get("description", "unknown"))
        return 1
    hook = info["result"]
    registered = hook.get("url") or ""

    expected = ""
    if base_url and secret:
        expected = f"{base_url.rstrip('/')}/telegram/webhook/{secret}"

    if not registered:
        report(FAIL, "webhook", "NONE registered — Telegram has nowhere to deliver to")
        print("\n  This is why the bot answers nothing, however correct your message was.")
        print("  Telegram only delivers to a registered webhook, and this app registers")
        print("  it at startup and only when BOT_TOKEN, BOT_WEBHOOK_SECRET and")
        print("  PUBLIC_BASE_URL are ALL set (app/main.py, lifespan).")
        missing = [
            name
            for name, value in (
                ("PUBLIC_BASE_URL", base_url),
                ("BOT_WEBHOOK_SECRET", secret),
            )
            if not value
        ]
        if missing:
            print(f"\n  Missing right now: {', '.join(missing)}")
        print("\n  Set what is missing, redeploy, then run this again — or run it with")
        print("  --fix to register the webhook immediately without a redeploy.")
    else:
        report(OK, "webhook", f"registered → {_mask_secret(registered, secret)}")
        if expected and registered != expected:
            report(
                WARN,
                "webhook url",
                "does NOT match this environment — likely left over from an earlier deploy",
            )
            print(f"\n  expected: {_mask_secret(expected, secret)}")
            print("  Run with --fix to point it at this deployment.")

    # 3. Delivery health — the part that is invisible without asking.
    pending = hook.get("pending_update_count", 0)
    if pending:
        report(WARN, "pending updates", f"{pending} queued and undelivered")
    else:
        report(OK, "pending updates", "0 — nothing stuck in Telegram's queue")

    last_error = hook.get("last_error_message")
    if last_error:
        when = _age(hook["last_error_date"]) if hook.get("last_error_date") else "unknown time"
        report(FAIL, "last delivery", f"FAILED ({when}) — {last_error}")
        print("\n  Telegram tried and could not reach this service. On Render's free plan")
        print("  the most common cause is the cold start: the instance sleeps after ~15")
        print("  minutes idle and takes about a minute to wake, which is longer than")
        print("  Telegram waits. The first message after a nap can be dropped, and the")
        print("  next one usually lands. If this error persists across a warm service,")
        print("  it is not the cold start — read the message above literally.")
    elif registered:
        report(OK, "last delivery", "no delivery error recorded")

    if args.fix:
        print()
        if not (base_url and secret):
            report(FAIL, "--fix", "needs PUBLIC_BASE_URL and BOT_WEBHOOK_SECRET set")
            return 1
        result = _call(token, "setWebhook", {"url": expected})
        if result.get("ok"):
            report(OK, "--fix", f"webhook set → {_mask_secret(expected, secret)}")
            print("\n  Send the bot a /start with a fresh link code now.")
        else:
            report(FAIL, "--fix", result.get("description", "unknown"))
            return 1

    print()
    if registered and not last_error:
        print("Webhook is registered and healthy. If the bot still does not answer, the")
        print("next suspect is the app itself — check the Render logs while you send a")
        print("message: a delivered update that errors shows up there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
