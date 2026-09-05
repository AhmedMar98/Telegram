"""How wide is the gap between ``links`` and ``resources``/``occurrences``?

This is a diagnostic, not a fix. It answers one question with real numbers
instead of an inference from reading code: since migration 0028 backfilled
``resources``/``occurrences`` once from whatever ``links`` held at that
moment, and nothing has written to either table since, how many rows has
``links`` grown by that ``resources``/``occurrences`` never learned about?

Every number below is a COUNT(*) or a MAX(created_at). Nothing that could
be a URL, a workspace name, or any other row content is ever read, held,
or printed — only integers and timestamps.

**Why this cannot be a single global query.** ``resources`` and
``occurrences`` carry FORCE ROW LEVEL SECURITY (``app.rls.PROTECTED_TABLES``);
``links`` does not (``app.rls.UNPROTECTED_BY_DESIGN``). A connection with
no tenant set reads *zero rows* from the first two — not an error, not a
warning, a silent empty result — which would report a "0% coverage" gap
regardless of what is actually there. Measured, not assumed: a first
version of this script did exactly that against a database seeded with a
known answer (5 links, 1 resource, 1 occurrence) and reported
``resources_total=0 occurrences_total=0 resource_coverage_pct=0.00``. The
real answer was 20.00. This version sums each count across every
workspace with the tenant set for each, using ``app.rls.for_each_workspace``
— the same mechanism a request or a scoped script uses.

Usage:
    DATABASE_URL=... python scripts/count_link_model_tables.py

Exit codes:
    0  ran to completion (the counts are the answer; there is no pass/fail)
    1  refused before querying, or a query failed
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import normalize_database_url  # noqa: E402
from app.rls import for_each_workspace  # noqa: E402


def _refuse(message: str) -> None:
    print(f"REFUSING: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        _refuse("DATABASE_URL is not set. Never pass a DSN as an argument — it carries the password.")

    engine = create_engine(normalize_database_url(dsn))

    with Session(engine) as db:

        def scalar(sql: str, **params: object) -> int:
            row = db.execute(text(sql), params).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        def one(sql: str, **params: object) -> object:
            row = db.execute(text(sql), params).fetchone()
            return row[0] if row is not None else None

        def timestamp(sql: str, **params: object) -> datetime | None:
            value = one(sql, **params)
            assert value is None or isinstance(value, datetime)
            return value

        db_name, login_role, effective_role = (
            one("SELECT current_database()"),
            one("SELECT session_user"),
            one("SELECT current_user"),
        )
        print(f"database={db_name} login_role={login_role} effective_role={effective_role}")

        # links carries no row-level security (app.rls.UNPROTECTED_BY_DESIGN),
        # so one global query is the true total — the same reasoning
        # scripts/check_link_vitality.py already relies on.
        links_total = scalar("SELECT count(*) FROM links")
        links_latest = timestamp("SELECT max(created_at) FROM links")
        print(f"links_total={links_total}")
        print(f"links_max_created_at={links_latest}")

        # resources and occurrences are FORCE row-level security
        # (app.rls.PROTECTED_TABLES). A query with no tenant set would see
        # zero rows in both — see the module docstring — so every count
        # against them, and every join that reads them, has to run once
        # per workspace with that workspace's tenant applied.
        resources_total = 0
        occurrences_total = 0
        links_without_resource = 0
        links_without_occurrence = 0
        dangling_occurrences = 0
        resources_latest: datetime | None = None
        occurrences_latest: datetime | None = None
        workspaces_seen = 0

        for workspace_id in for_each_workspace(db):
            workspaces_seen += 1
            resources_total += scalar("SELECT count(*) FROM resources WHERE workspace_id = :ws", ws=workspace_id)
            occurrences_total += scalar(
                "SELECT count(*) FROM occurrences WHERE workspace_id = :ws", ws=workspace_id
            )
            links_without_resource += scalar(
                "SELECT count(*) FROM links l "
                "WHERE l.workspace_id = :ws AND NOT EXISTS ("
                "  SELECT 1 FROM resources r WHERE r.workspace_id = :ws AND r.fingerprint = l.url_hash"
                ")",
                ws=workspace_id,
            )
            links_without_occurrence += scalar(
                "SELECT count(*) FROM links l "
                "WHERE l.workspace_id = :ws "
                "AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.workspace_id = :ws AND o.legacy_link_id = l.id)",
                ws=workspace_id,
            )
            dangling_occurrences += scalar(
                "SELECT count(*) FROM occurrences o "
                "WHERE o.workspace_id = :ws AND o.legacy_link_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM links l WHERE l.id = o.legacy_link_id)",
                ws=workspace_id,
            )
            ws_resources_latest = timestamp(
                "SELECT max(created_at) FROM resources WHERE workspace_id = :ws", ws=workspace_id
            )
            ws_occurrences_latest = timestamp(
                "SELECT max(created_at) FROM occurrences WHERE workspace_id = :ws", ws=workspace_id
            )
            if ws_resources_latest is not None and (
                resources_latest is None or ws_resources_latest > resources_latest
            ):
                resources_latest = ws_resources_latest
            if ws_occurrences_latest is not None and (
                occurrences_latest is None or ws_occurrences_latest > occurrences_latest
            ):
                occurrences_latest = ws_occurrences_latest

    print(f"workspaces_scanned={workspaces_seen}")
    print(f"resources_total={resources_total}")
    print(f"occurrences_total={occurrences_total}")
    print(f"links_without_matching_resource={links_without_resource}")
    print(f"links_without_matching_occurrence={links_without_occurrence}")
    print(f"dangling_occurrences_legacy_link_id_not_in_links={dangling_occurrences}")
    print(f"resources_max_created_at={resources_latest}")
    print(f"occurrences_max_created_at={occurrences_latest}")

    if links_total:
        coverage_pct = 100.0 * (links_total - links_without_resource) / links_total
        print(f"resource_coverage_pct={coverage_pct:.2f}")


if __name__ == "__main__":
    main()
