"""Import a browser, Pocket or Instapaper bookmark export. No API keys needed.

The second and third credential-free ways into the system (ideas 168 and
169), alongside the Telegram archive importer. A file you already have is
enough — no api_id, no session string, no login code.

How to produce the file:

  Chrome/Edge  -> Bookmarks -> Bookmark manager -> ⋮ -> Export bookmarks
  Firefox      -> Bookmarks -> Manage bookmarks -> Import and Backup ->
                  Export Bookmarks to HTML
  Safari       -> File -> Export -> Bookmarks
  Pocket       -> getpocket.com/export
  Instapaper   -> Settings -> Export -> Download .CSV file

Then:

  python scripts/import_bookmarks.py bookmarks.html --workspace 1

Several files at once is fine (idea 178) — they are imported in order and
reported together:

  python scripts/import_bookmarks.py chrome.html pocket.csv --workspace 1

Add --dry-run to see what would be imported without writing anything.

The format is detected from the file's contents, not its extension, so a
Pocket export saved as .txt still works.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bookmarks import ParseResult, parse  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.ingest import IMPORT_CHANNEL_PREFIX, IngestSummary, get_or_create_channel, ingest_text  # noqa: E402
from app.models import Workspace  # noqa: E402

logger = logging.getLogger("import-bookmarks")


def read_file(path: Path) -> str:
    """Read a bookmark file, tolerating the encodings exporters actually use.

    Old exports are frequently latin-1 or contain a stray byte from a
    truncated write. Replacement characters cost one mangled title;
    refusing to decode costs the entire import.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        sys.exit(f"error: no such file: {path}")
    except OSError as exc:
        sys.exit(f"error: cannot read {path}: {exc}")
    return raw.decode("utf-8", errors="replace")


def run(paths: list[Path], workspace_id: int, *, dry_run: bool) -> tuple[IngestSummary, ParseResult]:
    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            sys.exit(f"error: no workspace with id {workspace_id} — register an account first")

        summary = IngestSummary()
        totals = ParseResult()

        for path in paths:
            parsed = parse(read_file(path), filename=path.name)
            totals.skipped_unsupported_scheme += parsed.skipped_unsupported_scheme
            totals.skipped_malformed += parsed.skipped_malformed
            logger.info("%s: %d bookmark(s), %d skipped", path.name, len(parsed.bookmarks), parsed.total_skipped)
            if not parsed.bookmarks:
                continue

            # One channel per file, named after it. Bookmarks from a
            # browser and from Pocket are different collections with
            # different histories, and merging them into one bucket would
            # throw away the only provenance these formats carry.
            channel = get_or_create_channel(
                db,
                workspace_id=workspace_id,
                tg_channel_id=f"{IMPORT_CHANNEL_PREFIX}{path.name}",
                title=f"Bookmarks: {path.name}",
            )

            for index, bookmark in enumerate(parsed.bookmarks, start=1):
                ingest_text(
                    db,
                    workspace_id=workspace_id,
                    channel_id=channel.id,
                    text=bookmark.as_text(),
                    message_id=index,
                    posted_at=bookmark.added_at,
                    summary=summary,
                )

        if dry_run:
            db.rollback()
            logger.info("dry run — nothing was written")
        else:
            db.commit()
        return summary, totals
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", type=Path, nargs="+", help="one or more bookmark exports (HTML or CSV)")
    parser.add_argument("--workspace", type=int, required=True, help="workspace id to import into")
    parser.add_argument("--dry-run", action="store_true", help="report what would be imported, write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary, skipped = run(args.files, args.workspace, dry_run=args.dry_run)

    logger.info("found   %d link(s)", summary.total_found)
    logger.info("stored  %d new, skipped %d already present", summary.stored, summary.duplicates)
    if skipped.skipped_unsupported_scheme:
        # Named rather than lumped into one "skipped" count: bookmarklets
        # are the usual cause and are not a problem, so a person seeing a
        # shortfall should be able to tell that from a real failure.
        logger.info(
            "ignored %d entr(ies) that were not http/https (bookmarklets, browser-internal pages)",
            skipped.skipped_unsupported_scheme,
        )
    if skipped.skipped_malformed:
        logger.info("ignored %d unreadable row(s)", skipped.skipped_malformed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
