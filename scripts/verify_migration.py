"""Prove a database migration actually moved everything.

    SOURCE_DATABASE_URL=... TARGET_DATABASE_URL=... python scripts/verify_migration.py

``pg_restore`` reports success on a run that skipped rows. It exits 0 with
warnings on the console, and if the console has scrolled — or the restore
ran in a CI log nobody read — the migration looks finished. The failure
mode is the worst kind: everything works, the app starts, the dashboard
loads, and a fraction of the archive is simply gone. Nobody notices until
they search for something old.

This is the check that makes the cutover safe to commit to. It counts
every row of every table on both sides and refuses to report success on
any mismatch.

Both URLs come from the environment, never from arguments: a DSN carries
the database password, and arguments are readable by any other process on
the machine through the process list. Same rule as scripts/add_account.py.

Exit codes:
  0  every table matches — the migration is verified
  1  a mismatch, a missing table, or a connection failure
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, inspect, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.models  # noqa: E402,F401 - registers every table on Base.metadata
from app.config import normalize_database_url  # noqa: E402
from app.database import Base  # noqa: E402


def _counts(url: str, label: str) -> dict[str, int] | None:
    """Row count per table, or None if the database could not be read."""
    engine = create_engine(normalize_database_url(url))
    try:
        present = set(inspect(engine).get_table_names())
        counts: dict[str, int] = {}
        with Session(engine) as session:
            for name, table in sorted(Base.metadata.tables.items()):
                if name not in present:
                    # A missing table is not zero rows. Reporting it as 0
                    # would let "the table was never created" masquerade as
                    # "the table was empty on both sides", which is exactly
                    # the confusion this script exists to prevent.
                    counts[name] = -1
                    continue
                counts[name] = session.execute(select(func.count()).select_from(table)).scalar_one()
        return counts
    except SQLAlchemyError as exc:
        print(f"[FAIL] cannot read {label}: {type(exc).__name__}", file=sys.stderr)
        return None
    finally:
        engine.dispose()


def compare(source: dict[str, int], target: dict[str, int]) -> list[str]:
    """Human-readable problems, empty when the two sides agree."""
    problems: list[str] = []
    for name in sorted(set(source) | set(target)):
        left = source.get(name, -1)
        right = target.get(name, -1)
        if left == -1 and right == -1:
            # Neither side has it. A model that has no migration yet is a
            # separate problem from this one; not this script's business.
            continue
        if left == -1:
            problems.append(f"{name}: missing on the source, {right} row(s) on the target")
        elif right == -1:
            problems.append(f"{name}: {left} row(s) on the source, table missing on the target")
        elif left != right:
            problems.append(f"{name}: {left} row(s) on the source, {right} on the target ({right - left:+d})")
    return problems


def main() -> int:
    source_url = os.environ.get("SOURCE_DATABASE_URL", "").strip()
    target_url = os.environ.get("TARGET_DATABASE_URL", "").strip()
    if not source_url or not target_url:
        print(
            "Set SOURCE_DATABASE_URL and TARGET_DATABASE_URL in the environment.\n"
            "Never pass a database URL as an argument — it contains the password "
            "and arguments are visible in the process list.",
            file=sys.stderr,
        )
        return 1

    if source_url == target_url:
        print("[FAIL] both URLs point at the same database — nothing to verify.", file=sys.stderr)
        return 1

    source = _counts(source_url, "source")
    target = _counts(target_url, "target")
    if source is None or target is None:
        return 1

    print(f"{'table':<28} {'source':>10} {'target':>10}")
    print("-" * 50)
    for name in sorted(set(source) | set(target)):
        left = source.get(name, -1)
        right = target.get(name, -1)
        print(f"{name:<28} {('absent' if left == -1 else left):>10} {('absent' if right == -1 else right):>10}")

    problems = compare(source, target)
    print("-" * 50)
    if problems:
        print(f"\n{len(problems)} mismatch(es) — the migration is NOT verified:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nDo not retire the source database. Re-run the data restore for the "
            "affected tables and verify again.",
            file=sys.stderr,
        )
        return 1

    total = sum(count for count in source.values() if count > 0)
    print(f"\nVerified: {len(source)} tables, {total} rows, identical on both sides.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
