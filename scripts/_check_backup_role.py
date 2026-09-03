"""TEMPORARY, single-purpose diagnostic. See Incident 3 / RLS Backup Authority Check.

Runs exactly one fixed, read-only query against DATABASE_URL and prints
exactly three non-secret values: current_user, rolsuper, rolbypassrls.

No other query. No input of any kind — not a CLI argument, not an
environment variable, not a workflow input. Nothing here writes to the
database, changes a role, or changes a policy.

Deleted immediately after use, per the approval this diagnostic was run
under. If this file still exists, that deletion has not happened yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402

_QUERY = text(
    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;"
)


def main() -> None:
    with engine.connect() as conn:
        row = conn.execute(_QUERY).one()
    print(f"current_user={row[0]}")
    print(f"rolsuper={row[1]}")
    print(f"rolbypassrls={row[2]}")


if __name__ == "__main__":
    main()
