"""Put FORCE ROW LEVEL SECURITY back on a database restored from a backup.

Run this immediately after `pg_restore`, before anything is allowed to
connect. It is not optional and it is not a tidying step.

Why it has to exist: the backup is taken with FORCE briefly lifted — that
is the only way pg_dump will produce a complete dump of these tables — so
the dump faithfully records the state it saw, `FORCE = false`. Restore it
and you get every row, every policy, and `ENABLE ROW LEVEL SECURITY`
intact, but the owner's exemption back. The tenant policies are all still
there and still correct; they simply stop applying to the one role that
owns the tables, which in this deployment is the role the application
connects as.

So a restored database looks right, passes an eyeball check, has all its
policies listed — and isolates nothing from the application's own
connection. That is the exact failure shape this project treats as worse
than an outage, and it would arrive on the worst possible day, during a
recovery, when nobody is looking for it.

Measured, not assumed: on a real restore of this schema, ENABLE survives
18/18 and the policies survive, while FORCE survives 0/18.

    python scripts/rearm_force_rls.py "postgresql://..."

Exits non-zero unless every protected table ends up FORCE = true.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from app.rls import PROTECTED_TABLES  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: rearm_force_rls.py <dsn>", file=sys.stderr)
        raise SystemExit(1)
    dsn = sys.argv[1]

    with psycopg.connect(dsn, autocommit=True) as conn:
        present = {
            name
            for (name,) in conn.execute(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s)",
                (list(PROTECTED_TABLES),),
            ).fetchall()
        }
        absent = sorted(set(PROTECTED_TABLES) - present)
        if absent:
            print(
                f"protected tables missing from this database: {absent}. Refusing to "
                "report a re-armed database that is missing the tables the arming is for.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        for table in sorted(present):
            conn.execute(sql.SQL("ALTER TABLE public.{} FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))

        unforced = sorted(
            name
            for (name, forced) in conn.execute(
                "SELECT c.relname, c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(%s)",
                (list(PROTECTED_TABLES),),
            ).fetchall()
            if not forced
        )
        if unforced:
            print(f"FORCE still off on: {unforced}", file=sys.stderr)
            raise SystemExit(1)

    print(f"FORCE ROW LEVEL SECURITY re-armed on {len(present)}/{len(PROTECTED_TABLES)} tables")


if __name__ == "__main__":
    main()
