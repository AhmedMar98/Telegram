"""Report what the database actually holds, and generate its schema doc.

Two jobs that share one thing — the live metadata — so they share one
script rather than drifting apart in two:

  python scripts/db_report.py            # size and row counts
  python scripts/db_report.py --schema   # regenerate docs/12-schema.md

Answers the question the free plan makes urgent: how close is this to the
size limit, and which table is responsible? Every number is read from the
database; nothing is estimated.

Required environment:
  DATABASE_URL  - same database the web service uses
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.storage import database_bytes, largest_table  # noqa: E402

# Import for the side effect of registering every model on Base.metadata;
# without it the schema doc would silently describe an empty database.
import app.models  # noqa: E402, F401  isort:skip

SCHEMA_DOC = Path(__file__).resolve().parent.parent / "docs" / "12-schema.md"


def _human(size: int | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def report() -> int:
    db = SessionLocal()
    try:
        total = database_bytes(db)
        print(f"database size : {_human(total)}")
        print(f"largest table : {largest_table(db) or 'unknown'}")
        print()

        rows = []
        for table in Base.metadata.sorted_tables:
            count = db.execute(select(func.count()).select_from(table)).scalar() or 0
            rows.append((table.name, count))

        width = max(len(name) for name, _ in rows)
        for name, count in sorted(rows, key=lambda r: r[1], reverse=True):
            print(f"{name.ljust(width)}  {count:>10,}")

        if engine.dialect.name != "postgresql":
            print()
            print(f"note: size figures are Postgres-only; this run used {engine.dialect.name}.")
        return 0
    finally:
        db.close()


def write_schema_doc() -> int:
    """Generate the schema reference from the models themselves.

    Hand-written schema documentation is wrong the moment a column is
    added, and nothing detects it. Generating it means the doc can be
    regenerated and diffed — a stale one shows up as an uncommitted change.
    """
    lines = [
        "# مخطط قاعدة البيانات (مولَّد آلياً)",
        "",
        "> لا تحرّر هذا الملف يدوياً. أعد توليده بـ:",
        "> `python scripts/db_report.py --schema`",
        "",
        "المصدر هو `app/models.py` نفسه، فلا يمكن أن يصف عموداً غير موجود",
        "ولا أن يُغفل عموداً أُضيف.",
        "",
    ]

    for table in Base.metadata.sorted_tables:
        lines.append(f"## `{table.name}`")
        lines.append("")
        lines.append("| العمود | النوع | يقبل NULL | مفتاح |")
        lines.append("|---|---|---|---|")
        for column in table.columns:
            keys = []
            if column.primary_key:
                keys.append("PK")
            for fk in column.foreign_keys:
                keys.append(f"FK → {fk.target_fullname}")
            lines.append(
                f"| `{column.name}` | `{column.type}` | "
                f"{'نعم' if column.nullable else 'لا'} | {', '.join(keys) or '—'} |"
            )
        indexes = sorted(table.indexes, key=lambda i: i.name or "")
        if indexes:
            lines.append("")
            lines.append(
                "**الفهارس:** "
                + "، ".join(
                    f"`{index.name}` ({', '.join(c.name for c in index.columns)}){' فريد' if index.unique else ''}"
                    for index in indexes
                )
            )
        lines.append("")

    SCHEMA_DOC.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {SCHEMA_DOC.relative_to(Path.cwd())} ({len(Base.metadata.sorted_tables)} tables)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", action="store_true", help="regenerate docs/12-schema.md and exit")
    args = parser.parse_args()
    return write_schema_doc() if args.schema else report()


if __name__ == "__main__":
    raise SystemExit(main())
