"""Import links from a Telegram Desktop chat export. No API keys needed.

This is the credential-free path into the system. Telegram Desktop can
write a channel's whole history to JSON locally, which means links can be
collected without an api_id, an api_hash, a session string, or a login
code — useful for backfilling history, and for getting the platform
running before (or instead of) wiring up the automated collector.

How to produce the file:

  Telegram Desktop -> open the channel -> ... menu -> Export chat history
  -> uncheck the media boxes (only text is needed, so the export is small
  and fast) -> Format: JSON -> Export. You get a result.json.

Then:

  python scripts/import_telegram_export.py result.json --workspace 1

Add --dry-run to see what would be imported without writing anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.ingest import IMPORT_CHANNEL_PREFIX, IngestSummary, get_or_create_channel, ingest_text  # noqa: E402
from app.models import Workspace  # noqa: E402

logger = logging.getLogger("import")


def parse_message_text(raw: Any) -> tuple[str, list[str]]:
    """Flatten Telegram's text field and surface links hidden behind captions.

    ``text`` is either a plain string or a list mixing plain strings with
    entity objects. Entities of type ``text_link`` carry the real target in
    ``href`` while showing unrelated caption text, so those URLs would be
    lost entirely if only the visible text were scanned.
    """
    if isinstance(raw, str):
        return raw, []

    if not isinstance(raw, list):
        return "", []

    parts: list[str] = []
    hidden: list[str] = []
    for item in raw:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text", "")
            if isinstance(text, str):
                parts.append(text)
            href = item.get("href")
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                hidden.append(href)
    return "".join(parts), hidden


def parse_date(message: dict) -> datetime | None:
    raw = message.get("date")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "")).replace(tzinfo=None)
        except ValueError:
            pass
    unixtime = message.get("date_unixtime")
    if isinstance(unixtime, str | int):
        try:
            return datetime.fromtimestamp(int(unixtime), UTC).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            pass
    return None


def load_export(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"error: no such file: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path} is not valid JSON ({exc})")
    if not isinstance(payload, dict) or "messages" not in payload:
        sys.exit(
            f"error: {path} does not look like a Telegram export "
            "(expected a top-level object with a 'messages' array)"
        )
    return payload


def run(path: Path, workspace_id: int, *, dry_run: bool) -> IngestSummary:
    payload = load_export(path)
    messages = payload.get("messages") or []
    chat_name = payload.get("name") or "Imported chat"
    chat_id = payload.get("id")

    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            sys.exit(f"error: no workspace with id {workspace_id} — register an account first")

        channel = get_or_create_channel(
            db,
            workspace_id=workspace_id,
            tg_channel_id=f"{IMPORT_CHANNEL_PREFIX}{chat_id if chat_id is not None else chat_name}",
            title=chat_name,
        )

        summary = IngestSummary()
        for message in messages:
            if not isinstance(message, dict) or message.get("type") == "service":
                continue
            text, hidden = parse_message_text(message.get("text"))
            if not text and not hidden:
                continue
            ingest_text(
                db,
                workspace_id=workspace_id,
                channel_id=channel.id,
                text=text,
                message_id=int(message.get("id") or 0),
                posted_at=parse_date(message),
                extra_urls=hidden,
                summary=summary,
            )

        if dry_run:
            db.rollback()
            logger.info("dry run — nothing was written")
        else:
            db.commit()
        return summary
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export_file", type=Path, help="result.json from Telegram Desktop")
    parser.add_argument("--workspace", type=int, required=True, help="workspace id to import into")
    parser.add_argument("--dry-run", action="store_true", help="report what would be imported, write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = run(args.export_file, args.workspace, dry_run=args.dry_run)

    logger.info("scanned %d message(s)", summary.scanned)
    logger.info("found   %d link(s)", summary.total_found)
    logger.info("stored  %d new, skipped %d already present", summary.stored, summary.duplicates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
