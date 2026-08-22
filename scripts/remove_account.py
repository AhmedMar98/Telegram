"""Remove a collecting account safely.

The symmetric counterpart to ``add_account.py``. It exists because
removing an account is *not* a plain delete: channels reference the
account through a foreign key, so deleting one that still owns channels
fails outright with a ForeignKeyViolation — verified, not assumed.

What this does instead, and why:

1. **Reassigns the account's channels to the workspace default** by
   setting ``account_id = NULL``. That is already what NULL means
   everywhere else in this codebase (see ``ChannelUpdate``): "collected by
   the default account". So the channels keep collecting; they do not go
   quiet because an account was retired.
2. **Refuses to leave a workspace with no account at all.** Removing the
   last account silently stops all collection, which is a decision nobody
   makes by running a cleanup command.
3. **Reports what it will do before doing it**, and supports a dry run.

Required environment:
  DATABASE_URL

Usage:
  python scripts/remove_account.py --workspace-id 1 --label "second phone"
  python scripts/remove_account.py --workspace-id 1 --label "..." --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Channel, TelegramAccount  # noqa: E402
from app.rls import scope_session_to_workspace  # noqa: E402

logger = logging.getLogger("remove_account")


class RemovalRefused(Exception):
    """The removal would leave the workspace unable to collect."""


def remove_account(db: Session, *, workspace_id: int, label: str, dry_run: bool = False) -> dict[str, int]:
    """Detach the account's channels, then delete it. Returns what changed."""
    # Scoped here rather than in main() because this function is the unit
    # under test: a caller that reached it another way still gets the
    # tenant set, and the channel reassignment below runs under RLS.
    scope_session_to_workspace(db, workspace_id)

    account = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.label == label)
        .first()
    )
    if account is None:
        raise RemovalRefused(f"no account labelled {label!r} in workspace {workspace_id}")

    remaining = (
        db.query(TelegramAccount)
        .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.id != account.id)
        .count()
    )
    if remaining == 0:
        raise RemovalRefused(
            "refusing to remove the workspace's only collecting account — "
            "collection would stop entirely. Add a replacement first."
        )

    owned = db.query(Channel).filter(Channel.account_id == account.id).all()

    if dry_run:
        return {"channels_reassigned": len(owned), "accounts_removed": 0}

    # Reassign before delete, in one transaction: a crash between the two
    # would otherwise leave channels pointing at an account that is gone.
    for channel in owned:
        channel.account_id = None
    db.delete(account)
    db.commit()

    return {"channels_reassigned": len(owned), "accounts_removed": 1}


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Remove a collecting account.")
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--label", required=True, help="the account's label, as shown in /channels/accounts")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = remove_account(db, workspace_id=args.workspace_id, label=args.label, dry_run=args.dry_run)
    except RemovalRefused as exc:
        logger.error("%s", exc)
        return 1
    finally:
        db.close()

    verb = "would reassign" if args.dry_run else "reassigned"
    logger.info(
        "%s %d channel(s) to the default account; %s",
        verb,
        result["channels_reassigned"],
        "account not removed (dry run)" if args.dry_run else "account removed",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
