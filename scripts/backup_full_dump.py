"""A complete pg_dump, taken inside the shortest FORCE-RLS window that works.

pg_dump sets ``row_security = off`` on purpose: a backup either contains
every row or it errors, because a dump that silently held a filtered
subset would look exactly like a good one until the day it was needed.
``FORCE ROW LEVEL SECURITY`` removes the owner's usual exemption, so this
role — the table owner, not a superuser, without BYPASSRLS — has no route
to a complete logical dump while FORCE is on. Rendered plainly: with FORCE
set on eighteen tables, `pg_dump` refuses, and the only alternatives
PostgreSQL documents are ``--enable-row-security`` (a partial dump wearing
a complete dump's name) or ``-T`` (the same thing, chosen by hand).

Neither is acceptable here, so this does the third thing: lift FORCE on
exactly the tables ``app.rls.PROTECTED_TABLES`` names, dump, and put FORCE
back — verified afterwards rather than assumed, and attempted whether or
not the dump itself succeeded. It is the same pattern migration 0028
already uses to run its own backfill against these tables, for the same
reason.

The table list is imported, never retyped. A hand-copied list is a list
that goes stale the first time somebody adds a tenant table and forgets
this file; an imported one turns that into a loud stop instead. The count
is asserted too, so a change to the protected set halts the operation for
a human rather than silently widening what gets unlocked.

What this deliberately does not do: encrypt, upload, or clean up. It
writes one custom-format dump at the path it is given and exits. The
workflow around it owns everything else.

Usage:
    python scripts/backup_full_dump.py "$DATABASE_URL" backup.dump

Exit codes:
    0  dump written, FORCE restored and verified on every table
    1  refused before the window, or the dump failed (FORCE restored)
    2  the dump ran but FORCE could NOT be restored — a live security
       incident, not a failed backup. Loud on purpose.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from app.rls import PROTECTED_TABLES  # noqa: E402

# Authorised scope for the window, agreed against the imported list rather
# than a number written from memory — the first attempt at this operation
# was stopped by exactly this check, because a hand count had said 17.
EXPECTED_TABLE_COUNT = 18


def _force_state(conn: psycopg.Connection) -> dict[str, bool]:
    """``relforcerowsecurity`` for each protected table, by name."""
    rows = conn.execute(
        "SELECT c.relname, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s)",
        (list(PROTECTED_TABLES),),
    ).fetchall()
    return dict(rows)


def _set_force(conn: psycopg.Connection, table: str, *, forced: bool) -> None:
    clause = "FORCE" if forced else "NO FORCE"
    conn.execute(sql.SQL("ALTER TABLE public.{} " + clause + " ROW LEVEL SECURITY").format(sql.Identifier(table)))


def _refuse(message: str) -> NoReturn:
    print(f"REFUSING: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) != 3:
        _refuse("usage: backup_full_dump.py <dsn> <dump-path>")
    dsn, dump_path = sys.argv[1], sys.argv[2]

    if len(PROTECTED_TABLES) != EXPECTED_TABLE_COUNT:
        _refuse(
            f"app.rls.PROTECTED_TABLES lists {len(PROTECTED_TABLES)} tables, not the "
            f"{EXPECTED_TABLE_COUNT} this operation is authorised for. The protected "
            "set changed; the authorisation has not. A human decides which it is."
        )

    # ---- preflight, before anything is unlocked -------------------------
    with psycopg.connect(dsn, autocommit=True) as conn:
        ident = conn.execute("SELECT current_database(), current_user, version()").fetchone()
        if ident is None:  # pragma: no cover - a server that answers nothing
            _refuse("the server did not answer an identity query")
        print(f"database={ident[0]} user={ident[1]}")
        print(f"server={ident[2].split(',')[0]}")

        baseline = _force_state(conn)
        absent = sorted(set(PROTECTED_TABLES) - set(baseline))
        if absent:
            _refuse(f"protected tables not present in this database: {absent}")

        unforced = sorted(name for name, forced in baseline.items() if not forced)
        if unforced:
            _refuse(
                f"expected every protected table to start FORCE=true; these are "
                f"already unlocked: {unforced}. That is not the state this operation "
                "was authorised against, and it may be someone else's window."
            )
        print(f"baseline: {len(baseline)}/{EXPECTED_TABLE_COUNT} tables FORCE=true")

        # Not a gate, a record: "the app is down" is not evidence that
        # nothing else holds a connection, and whoever reads this log later
        # deserves to know what else was talking to the database.
        sessions = conn.execute(
            "SELECT pid, usename, COALESCE(application_name, ''), state "
            "FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchall()
        print(f"other_sessions={len(sessions)}")
        for pid, usename, appname, state in sessions:
            print(f"  pid={pid} user={usename} app={appname!r} state={state}")

        # ---- open the window --------------------------------------------
        for table in PROTECTED_TABLES:
            _set_force(conn, table, forced=False)

        opened = _force_state(conn)
        still_forced = sorted(name for name, forced in opened.items() if forced)
        if still_forced:
            for table in still_forced:
                _set_force(conn, table, forced=True)
            _refuse(f"NO FORCE did not take on {still_forced}; reverted what changed and did not start the dump.")
        print(f"window OPEN: {len(opened)}/{EXPECTED_TABLE_COUNT} tables NO FORCE (verified)")

    # ---- the dump itself, with the window open --------------------------
    dump_failure: BaseException | None = None
    try:
        subprocess.run(
            ["pg_dump", dsn, "--no-owner", "--no-privileges", "-F", "c", "-f", dump_path],
            check=True,
        )
        print("pg_dump: OK")
    except BaseException as exc:  # noqa: BLE001 — FORCE goes back regardless
        dump_failure = exc
        print(f"pg_dump: FAILED ({exc})", file=sys.stderr)
    finally:
        # A fresh connection: whatever went wrong above may have been the
        # connection itself, and this is the step that must not be skipped.
        with psycopg.connect(dsn, autocommit=True) as conn:
            for table in PROTECTED_TABLES:
                _set_force(conn, table, forced=True)
            closed = _force_state(conn)
            not_restored = sorted(name for name, forced in closed.items() if not forced)
            restored = len(closed) - len(not_restored)
            print(f"window CLOSED: {restored}/{EXPECTED_TABLE_COUNT} tables FORCE=true (verified)")
            if not_restored:
                print(
                    "SECURITY RESTORATION FAILED on: "
                    f"{not_restored}. Row-level security is NOT enforced on these "
                    "tables right now. Treat this as an open incident, not a failed "
                    "backup.",
                    file=sys.stderr,
                )
                raise SystemExit(2)

    if dump_failure is not None:
        raise SystemExit(1)

    size = Path(dump_path).stat().st_size
    if size == 0:
        _refuse(f"pg_dump exited 0 but {dump_path} is empty")
    print(f"FULL DUMP: {dump_path} ({size} bytes), FORCE restored {EXPECTED_TABLE_COUNT}/{EXPECTED_TABLE_COUNT}")


if __name__ == "__main__":
    main()
