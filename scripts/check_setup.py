"""Diagnose whether this deployment is actually wired up correctly.

Run it locally, or from GitHub Actions via the "Verify setup" workflow,
to find out exactly which piece is missing instead of waiting for the
hourly collector to quietly do nothing.

    python scripts/check_setup.py

Every check reports OK / WARN / FAIL independently, so one missing secret
does not hide the state of everything else. Exit code is non-zero only if
something is genuinely broken; optional-but-absent features are warnings.
Secrets are never printed — only whether they are present and usable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_SYMBOL = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
_DEFAULT_FIELD_ENCRYPTION_KEY = "S7uvgQ59s2Xo-V2u3yZdnqZLxhnienyS6rirAOJ_pnA="

results: list[tuple[str, str, str]] = []


def report(status: str, check: str, detail: str) -> None:
    results.append((status, check, detail))
    print(f"{_SYMBOL[status]} {check}: {detail}")


def check_core_env() -> bool:
    ok = True
    if os.environ.get("DATABASE_URL"):
        report(OK, "DATABASE_URL", "set")
    else:
        report(FAIL, "DATABASE_URL", "missing — the app and collector cannot reach any database")
        ok = False

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        report(FAIL, "SECRET_KEY", "missing")
        ok = False
    elif secret in {"dev-secret-key-change-me", "dev-secret", "changeme"} or len(secret) < 16:
        report(WARN, "SECRET_KEY", "set but looks like a default/short value — use a long random string")
    else:
        report(OK, "SECRET_KEY", "set")
    return ok


def check_database() -> bool:
    from app.database import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        report(FAIL, "database", f"cannot connect ({type(exc).__name__})")
        return False
    report(OK, "database", f"reachable ({engine.dialect.name})")

    tables = set(inspect(engine).get_table_names())
    expected = {"workspaces", "users", "channels", "links", "audit_log", "login_attempts"}
    missing = expected - tables
    if missing:
        report(FAIL, "schema", f"missing tables: {', '.join(sorted(missing))} — run `alembic upgrade head`")
        return False
    report(OK, "schema", f"{len(expected)} core tables present")
    return True


def check_workspace_and_channels() -> bool:
    from app.database import SessionLocal
    from app.models import Channel, Link, User, Workspace

    raw_id = os.environ.get("COLLECTOR_WORKSPACE_ID")
    db = SessionLocal()
    try:
        total_workspaces = db.query(Workspace).count()
        if total_workspaces == 0:
            report(FAIL, "workspace", "no workspace exists yet — register an account on the web UI first")
            return False

        if not raw_id:
            report(WARN, "COLLECTOR_WORKSPACE_ID", "not set — the collector will not know what to feed")
            return True
        try:
            workspace_id = int(raw_id)
        except ValueError:
            report(FAIL, "COLLECTOR_WORKSPACE_ID", f"{raw_id!r} is not a number")
            return False

        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            report(FAIL, "COLLECTOR_WORKSPACE_ID", f"no workspace with id {workspace_id}")
            return False
        users = db.query(User).filter(User.workspace_id == workspace_id).count()
        report(OK, "workspace", f"id {workspace_id} ({workspace.name!r}), {users} user(s)")

        active = (
            db.query(Channel).filter(Channel.workspace_id == workspace_id, Channel.is_active.is_(True)).count()
        )
        if active == 0:
            report(WARN, "channels", "no active channels — add them in the dashboard, or nothing is collected")
        else:
            report(OK, "channels", f"{active} active")

        report(OK, "links", f"{db.query(Link).filter(Link.workspace_id == workspace_id).count()} collected so far")
        return True
    finally:
        db.close()


def check_telegram_credentials() -> bool:
    present = [name for name in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING") if os.environ.get(name)]
    if not present:
        report(WARN, "telegram", "no collector credentials set — collection is not running yet")
        return True
    missing = {"TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING"} - set(present)
    if missing:
        report(FAIL, "telegram", f"partially configured, missing: {', '.join(sorted(missing))}")
        return False

    try:
        int(os.environ["TG_API_ID"])
    except ValueError:
        report(FAIL, "TG_API_ID", "is not a number")
        return False

    session = os.environ["TG_SESSION_STRING"]
    if len(session) < 100:
        report(FAIL, "TG_SESSION_STRING", "too short to be a real session — regenerate it")
        return False
    report(OK, "telegram", "credentials present and well-formed (not verified against Telegram here)")
    return True


def check_optional_features() -> None:
    bot_token = os.environ.get("BOT_TOKEN")
    base_url = os.environ.get("PUBLIC_BASE_URL")
    webhook_secret = os.environ.get("BOT_WEBHOOK_SECRET")
    if not bot_token:
        report(WARN, "bot", "BOT_TOKEN not set — the Telegram bot is disabled (the web UI still works)")
    elif not (base_url and webhook_secret):
        needed = [n for n, v in (("PUBLIC_BASE_URL", base_url), ("BOT_WEBHOOK_SECRET", webhook_secret)) if not v]
        report(WARN, "bot", f"BOT_TOKEN set but {', '.join(needed)} missing — the webhook cannot register")
    else:
        if not base_url.startswith("https://"):
            report(FAIL, "bot", "PUBLIC_BASE_URL must be https — Telegram refuses plain http webhooks")
        else:
            report(OK, "bot", "token, public URL and webhook secret all set")

    if os.environ.get("GROQ_API_KEY"):
        report(OK, "llm tier", "GROQ_API_KEY set — low-confidence links get a second opinion")
    else:
        report(WARN, "llm tier", "GROQ_API_KEY not set — rules-only classification (still fully functional)")

    if os.environ.get("INVITE_CODE"):
        report(OK, "registration", "gated by INVITE_CODE")
    else:
        report(WARN, "registration", "INVITE_CODE not set — anyone reaching the URL can create an account")

    field_key = os.environ.get("FIELD_ENCRYPTION_KEY")
    if field_key and field_key != _DEFAULT_FIELD_ENCRYPTION_KEY:
        report(OK, "field encryption", "FIELD_ENCRYPTION_KEY set to a non-default value")
    else:
        report(
            WARN,
            "field encryption",
            "FIELD_ENCRYPTION_KEY unset or using the published dev default — collected Telegram "
            "session strings are only decoratively encrypted until a real secret is set",
        )


def main() -> int:
    print("Setup check\n" + "=" * 72)
    healthy = check_core_env()
    if healthy:
        healthy = check_database()
        if healthy:
            check_workspace_and_channels()
        healthy = check_telegram_credentials() and healthy
    check_optional_features()

    failures = [c for status, c, _ in results if status == FAIL]
    warnings = [c for status, c, _ in results if status == WARN]
    print("=" * 72)
    print(
        f"{len(results) - len(failures) - len(warnings)} ok, {len(warnings)} warning(s), {len(failures)} failure(s)"
    )
    if failures:
        print("\nMust fix: " + ", ".join(failures))
        return 1
    print("\nNo blocking problems found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
