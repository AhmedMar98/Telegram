"""Diagnose whether this deployment is actually wired up correctly.

Run it locally, or from GitHub Actions via the "Verify setup" workflow,
to find out exactly which piece is missing instead of waiting for the
hourly collector to quietly do nothing.

    python scripts/check_setup.py

Every check reports OK / WARN / FAIL independently, so one missing secret
does not hide the state of everything else. Exit code is non-zero only if
something is genuinely broken; optional-but-absent features are warnings.
Secrets are never printed — only whether they are present and usable.

A handful of checks below escalate from WARN to FAIL specifically when
``ENVIRONMENT=production``: a published default that is merely a bad idea
in development (which runs on it by design) is a real vulnerability once
it is what a public deployment actually signs cookies or encrypts secrets
with. ``check_production_secrets`` in particular exists to answer one
question before a deploy does: will app/main.py's lifespan actually start
with what is set right now?
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.config import _PUBLISHED_DEFAULTS, Settings, production_secrets_check  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_SYMBOL = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
# Kept as a module-level name (rather than inlined at each call site)
# because tests/test_check_setup.py references it directly, and because
# app.config._PUBLISHED_DEFAULTS is the single source of truth this name
# now points at — the two cannot drift apart.
_DEFAULT_FIELD_ENCRYPTION_KEY = _PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"]

# A bearer credential below this many characters is treated as weak. This
# is a length floor, not a real entropy estimate — a single string has no
# entropy to measure, that needs a distribution to sample from. 32 chars
# of the token_urlsafe alphabet is roughly 192 bits; this floor exists to
# catch "short", not to certify "strong".
_WEAK_SECRET_LENGTH = 32

results: list[tuple[str, str, str]] = []


def report(status: str, check: str, detail: str) -> None:
    results.append((status, check, detail))
    print(f"{_SYMBOL[status]} {check}: {detail}")


def _is_production() -> bool:
    return os.environ.get("ENVIRONMENT", "development") == "production"


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
    elif secret == _PUBLISHED_DEFAULTS["SECRET_KEY"]:
        # The exact value app/main.py's lifespan also checks. FAIL here in
        # production means the deployment would not have booted; WARN in
        # development is correct, since development runs on this value by
        # design and the test suite depends on that.
        if _is_production():
            report(FAIL, "SECRET_KEY", "the published default — lifespan will refuse to start in production")
            ok = False
        else:
            report(WARN, "SECRET_KEY", "the published dev default — override before deploying")
    elif len(secret) < 16:
        report(WARN, "SECRET_KEY", "set but shorter than 16 chars — use a long random string")
    else:
        report(OK, "SECRET_KEY", "set")
    return ok


def check_database_url_scheme() -> None:
    """A sqlite:// URL in production is a defect, not a deployment choice.

    The file lives on Render's ephemeral filesystem — every redeploy
    recreates the container from the image, and whatever was in the file
    is gone. A development database on SQLite is normal; a production one
    is data loss waiting for the next push to main.

    Runs regardless of whether DATABASE_URL is reachable (check_database
    below returns False and short-circuits the rest of the chain on a
    connection failure), so a misconfigured scheme is reported even when
    the database it points at cannot be reached at all.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return  # already reported by check_core_env

    is_sqlite = url.startswith("sqlite")
    if is_sqlite and _is_production():
        report(
            FAIL,
            "DATABASE_URL scheme",
            "sqlite:// in production — the file is lost on every redeploy. Use a managed Postgres URL",
        )
    elif is_sqlite:
        report(
            WARN, "DATABASE_URL scheme", "sqlite:// — fine for local development, lost on redeploy in production"
        )
    else:
        report(OK, "DATABASE_URL scheme", "postgres (durable across redeploys)")


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


def check_row_level_security() -> None:
    """Is tenant isolation actually being enforced by the database?

    This cannot be answered by looking at the migration, and that is the
    whole point of asking it here. Three separate conditions each reduce
    row-level security to decoration while ``pg_class`` still says it is
    enabled:

    - the engine is SQLite, which has no row-level security at all;
    - the connecting role is a **superuser**, which bypasses RLS
      unconditionally — FORCE included;
    - a table is ENABLEd but not FORCEd, so its owner bypasses it, and the
      application's role *is* the owner.

    A managed provider decides which role you get, so the superuser case
    is a real deployment outcome and not a hypothetical. app/rls.py refuses
    to claim a protection it has not confirmed; this is where a deployment
    gets to confirm it without opening a Python prompt.
    """
    from app.database import SessionLocal
    from app.rls import PROTECTED_TABLES, rls_effective

    db = SessionLocal()
    try:
        status = rls_effective(db)
    except SQLAlchemyError as exc:
        report(WARN, "row-level security", f"could not be determined ({type(exc).__name__})")
        return
    finally:
        db.close()

    reason = status["reason"]
    if not status["supported"]:
        report(
            WARN,
            "row-level security",
            "not available on this engine (SQLite) — isolation here rests on the application's "
            "workspace_id filters alone, which is expected in development",
        )
    elif reason == "effective":
        report(
            OK, "row-level security", f"enforced on {len(PROTECTED_TABLES)} tables, connected as a non-superuser"
        )
    elif reason == "superuser_bypasses_rls":
        report(
            WARN,
            "row-level security",
            "the database user is a SUPERUSER, which bypasses row-level security entirely — the "
            "policies exist and enforce nothing. Application-level filtering still applies; "
            "database-level isolation does not",
        )
    elif reason == "tables_not_forced":
        report(
            FAIL,
            "row-level security",
            f"enabled but NOT forced on: {', '.join(status['unforced_tables'])} — the owning role "
            "bypasses an unforced policy, so these tables are unprotected. Re-run `alembic upgrade head`",
        )
    else:
        report(
            FAIL,
            "row-level security",
            f"missing on: {', '.join(status['tables_without_rls'])} — run `alembic upgrade head`",
        )


def check_workspace_and_channels() -> bool:
    from app.config import get_settings
    from app.database import SessionLocal
    from app.dialogs import parse_scope
    from app.models import Channel, Link, User, Workspace
    from app.rls import scope_session_to_workspace

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

        # The channel count below reads a table under row-level security.
        # Unscoped it would report "no active channels" on a perfectly
        # healthy deployment — a diagnostic that lies is worse than none.
        scope_session_to_workspace(db, workspace_id)

        users = db.query(User).filter(User.workspace_id == workspace_id).count()
        report(OK, "workspace", f"id {workspace_id} ({workspace.name!r}), {users} user(s)")

        active = (
            db.query(Channel).filter(Channel.workspace_id == workspace_id, Channel.is_active.is_(True)).count()
        )
        settings = get_settings()
        kinds = ", ".join(sorted(parse_scope(settings.collector_scope)))
        if settings.collector_auto_discover:
            # With discovery on, an empty channel list is the normal state
            # of a fresh deployment, not a misconfiguration: the first run
            # registers whatever the account can see. Reporting it as a
            # warning would train the reader to ignore this line.
            report(OK, "discovery", f"on — the collector registers the account's dialogs itself (kinds: {kinds})")
            report(OK if active else WARN, "channels", f"{active} active (the first run adds the rest)")
        else:
            report(OK, "discovery", "off — only dialogs added by hand are collected")
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
        elif len(webhook_secret) < _WEAK_SECRET_LENGTH:
            # Telegram accepts 1-256 chars in secret_token; the value is
            # hashed to sha256 hex before being sent (app/bot/telegram_bot.py
            # webhook_token()), but hashing does not add entropy the
            # operator did not choose. A short secret is a bearer
            # credential anyone who can guess it can use to post fake
            # updates to the webhook route.
            if _is_production():
                report(
                    FAIL,
                    "webhook secret",
                    f"BOT_WEBHOOK_SECRET is {len(webhook_secret)} chars — below the {_WEAK_SECRET_LENGTH}-char "
                    'floor for a production bearer credential. Regenerate with: python -c "import secrets; '
                    'print(secrets.token_urlsafe(48))"',
                )
            else:
                report(
                    WARN,
                    "webhook secret",
                    f"BOT_WEBHOOK_SECRET is {len(webhook_secret)} chars — short. Acceptable for "
                    "development, regenerate before production",
                )
        else:
            report(OK, "bot", "token, public URL and webhook secret all set")

    # The session cookie's Secure flag is *conditional on configuration*
    # (`secure=settings.environment == "production"`), so a service that
    # serves HTTPS without ENVIRONMENT=production sends its session cookie
    # marked as safe over plain HTTP — silently, with nothing on screen to
    # say so. render.yaml sets it; a service created by hand may not.
    environment = os.environ.get("ENVIRONMENT", "development")
    base_url = os.environ.get("PUBLIC_BASE_URL") or ""
    if environment == "production":
        report(OK, "session cookie", "HttpOnly, SameSite=Lax, Secure (ENVIRONMENT=production)")
    elif base_url.startswith("https://"):
        report(
            FAIL,
            "session cookie",
            f"PUBLIC_BASE_URL is https but ENVIRONMENT={environment!r} — the session cookie will be "
            "sent WITHOUT Secure, so any accidental http:// request leaks it. Set ENVIRONMENT=production",
        )
    else:
        report(WARN, "session cookie", f"ENVIRONMENT={environment!r} — Secure flag off (correct for local http)")

    # The daily backup refuses to run without this, so an unset value is a
    # backup that is not happening at all — and the failure surfaces only
    # in the Actions log, which nobody reads until they need a restore.
    if os.environ.get("BACKUP_PASSPHRASE"):
        report(OK, "backup encryption", "BACKUP_PASSPHRASE set — the daily dump is encrypted")
    else:
        report(
            WARN,
            "backup encryption",
            "BACKUP_PASSPHRASE not set — backup.yml will fail rather than upload a plaintext "
            "dump, so there is currently no backup at all",
        )

    invite = os.environ.get("INVITE_CODE")
    # Common guessable words are the same as no gate at all — checked
    # first, and separately from the length check, so the message names
    # the actual reason rather than lumping "invite" in with a random
    # 6-char string that merely happens to be short.
    _GUESSABLE_INVITE_CODES = {"invite", "invite-only", "secret", "password", "1234", "123456", "code"}
    if not invite:
        report(WARN, "registration", "INVITE_CODE not set — anyone reaching the URL can create an account")
    elif invite.lower() in _GUESSABLE_INVITE_CODES:
        report(
            WARN, "registration", f"INVITE_CODE is a common guessable value ({invite!r}) — nominally gated only"
        )
    elif len(invite) < 8:
        report(WARN, "registration", f"INVITE_CODE is {len(invite)} chars — short for a gating credential")
    else:
        report(OK, "registration", "gated by INVITE_CODE")

    # WARN in development (decorative encryption is still a working
    # default there); FAIL in production, where app/main.py's lifespan
    # will refuse to start on the same value — this diagnostic must not
    # pass what the runtime rejects.
    field_key = os.environ.get("FIELD_ENCRYPTION_KEY")
    is_default = (not field_key) or field_key == _DEFAULT_FIELD_ENCRYPTION_KEY
    if not is_default:
        report(OK, "field encryption", "FIELD_ENCRYPTION_KEY set to a non-default value")
    elif _is_production():
        report(
            FAIL,
            "field encryption",
            "FIELD_ENCRYPTION_KEY unset or using the published dev default in production — lifespan will "
            'refuse to start. Generate with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"',
        )
    else:
        report(
            WARN,
            "field encryption",
            "FIELD_ENCRYPTION_KEY unset or using the published dev default — collected Telegram "
            "session strings are only decoratively encrypted until a real secret is set",
        )


def check_production_secrets() -> None:
    """The check that closes the loop with app/main.py's lifespan.

    lifespan calls production_secrets_check() and raises if any secret is
    still the published default in ENVIRONMENT=production. This runs the
    identical check ahead of a deploy, so the defect shows up here — or in
    CI — rather than in a restart-looping deploy log.

    Kept separate from check_core_env rather than folded into it: that one
    asks "is each secret present and reasonable on its own?"; this one
    asks "would the app actually boot with what is set right now?". The
    two can disagree — a SECRET_KEY that merely resembles the published
    default (short, guessable) still passes this check, because only an
    exact match is what lifespan itself tests for.
    """
    settings = Settings()
    problems = production_secrets_check(settings)
    if not problems:
        if _is_production():
            report(OK, "production secrets", "lifespan will start — no published defaults in production")
        # In development this is a no-op by design: production_secrets_check
        # returns [] unconditionally there. Reporting OK for a check that
        # did not actually run would be its own small lie.
        return

    report(
        FAIL,
        "production secrets",
        f"lifespan will refuse to start: {', '.join(problems)} still at the published default. "
        "Override via environment variable(s) before redeploying",
    )


def check_scheduled_job_secrets() -> None:
    """What the *scheduled* jobs need beyond what the web service needs.

    Deliberately narrow, because it would be easy to make it wide and
    wrong: ``BACKUP_PASSPHRASE`` and ``FIELD_ENCRYPTION_KEY`` are already
    reported by check_optional_features, and a second line about the same
    secret in different words is how a diagnostic teaches people to skim
    it. What is *not* covered anywhere else is what this adds:

    - **The status-board pair.** ``APP_BASE_URL`` and ``APP_API_KEY``
      together are what lets a scheduled run report its outcome. Neither
      set is a choice; exactly one set is a typo that cannot work. And an
      unreported run leaves the board empty — which reads exactly like
      every run having stopped, the one ambiguity the board exists to
      remove.
    - **Whether the encryption key is usable at all**, as opposed to
      merely non-default. A key that is set but malformed reads as
      configured everywhere until an hourly collector fails on it.
    - **The thing no single side can check**: whether this copy of
      ``FIELD_ENCRYPTION_KEY`` is the *same* value the web service holds.
      It has to be — that copy encrypted the rows this one must read — and
      saying so is the difference between a diagnostic and a false
      reassurance.

    Nothing here prints a value. Presence, shape and length only.
    """
    key = os.environ.get("FIELD_ENCRYPTION_KEY", "")
    if key and key != _DEFAULT_FIELD_ENCRYPTION_KEY:
        try:
            from cryptography.fernet import Fernet

            Fernet(key.encode())
        except Exception:  # noqa: BLE001 - any rejection means the same thing to the reader
            report(
                FAIL,
                "FIELD_ENCRYPTION_KEY",
                "set but not a valid Fernet key — every job that decrypts a session string will fail on it. "
                'Regenerate with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"',
            )
        else:
            report(
                OK,
                "FIELD_ENCRYPTION_KEY",
                "well-formed — but whether it is the same value the web service uses cannot be checked "
                "from here, and it has to be: that copy encrypted the rows this one reads",
            )

    base = os.environ.get("APP_BASE_URL", "")
    api_key = os.environ.get("APP_API_KEY", "")
    if not base and not api_key:
        report(
            WARN,
            "status board",
            "APP_BASE_URL and APP_API_KEY not set — scheduled runs cannot report their outcome, so the "
            "board stays empty, which reads exactly like every run having stopped",
        )
    elif not (base and api_key):
        missing = "APP_API_KEY" if base else "APP_BASE_URL"
        report(FAIL, "status board", f"{missing} is missing while the other is set — reporting cannot work")
    elif not base.startswith("https://"):
        report(FAIL, "APP_BASE_URL", "must be https — the API key would otherwise travel in clear text")
    elif not api_key.startswith("lipk_"):
        report(
            WARN,
            "APP_API_KEY",
            'does not start with "lipk_" — dashboard-issued keys do, so this may be the wrong value',
        )
    else:
        report(OK, "status board", "APP_BASE_URL and APP_API_KEY set — scheduled runs can report in")


def main() -> int:
    print("Setup check\n" + "=" * 72)
    healthy = check_core_env()
    check_database_url_scheme()
    if healthy:
        healthy = check_database()
        if healthy:
            check_row_level_security()
            check_workspace_and_channels()
        healthy = check_telegram_credentials() and healthy
    check_optional_features()
    check_scheduled_job_secrets()
    check_production_secrets()

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
