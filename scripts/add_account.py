"""Register an additional Telegram account for collection.

The first collecting account is bootstrapped automatically from
``TG_SESSION_STRING`` by scripts/collect.py. Every account after that is
registered here, so that adding a second or third account never requires
putting another credential into the collector workflow's environment.

    TG_SESSION_STRING="1BVtsOK..." python scripts/add_account.py --label "second"

The session string is read from the environment rather than a command-line
argument on purpose: arguments are visible to every other process on the
machine through the process list, and land in shell history. It is
encrypted with app/crypto.py before it touches the database, exactly as
the collector does for the primary account.

Required environment:
  DATABASE_URL          - the same database the web service uses
  TG_SESSION_STRING     - the new account's Telethon StringSession
  FIELD_ENCRYPTION_KEY  - must match the collector's, or the stored row
                          cannot be decrypted at collection time
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.crypto import encrypt_field  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import TelegramAccount, Workspace  # noqa: E402

# Shorter than any real StringSession; catches an empty or truncated paste
# before it becomes a row that fails at collection time instead.
MIN_SESSION_LENGTH = 100


def add_account(workspace_id: int, label: str, session_string: str) -> int:
    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            raise SystemExit(f"no workspace with id {workspace_id}")

        clash = (
            db.query(TelegramAccount)
            .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.label == label)
            .first()
        )
        if clash is not None:
            raise SystemExit(f"workspace {workspace_id} already has an account labelled {label!r}")

        # Not a licensing limit: every account is a real Telegram login
        # whose session string sits encrypted in this database, so an
        # unbounded number is an unbounded pile of bearer credentials.
        limit = get_settings().max_accounts_per_workspace
        existing = db.query(TelegramAccount).filter(TelegramAccount.workspace_id == workspace_id).count()
        if existing >= limit:
            raise SystemExit(
                f"workspace {workspace_id} already has {existing} accounts "
                f"(limit {limit}). Raise MAX_ACCOUNTS_PER_WORKSPACE, or remove one "
                f"with scripts/remove_account.py."
            )

        account = TelegramAccount(
            workspace_id=workspace_id, label=label, session_string=encrypt_field(session_string)
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account.id
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=int, required=True, help="workspace id this account collects for")
    parser.add_argument("--label", required=True, help="human-readable name, unique within the workspace")
    args = parser.parse_args()

    session_string = os.environ.get("TG_SESSION_STRING", "").strip()
    if not session_string:
        raise SystemExit("TG_SESSION_STRING is not set (never pass a session string as an argument)")
    if len(session_string) < MIN_SESSION_LENGTH:
        raise SystemExit("TG_SESSION_STRING is too short to be a real session — regenerate it")

    account_id = add_account(args.workspace, args.label, session_string)
    # Never print the session string itself, only that it was stored.
    print(f"registered account id={account_id} label={args.label!r} for workspace {args.workspace}")
    print(f'Assign channels to it with: PATCH /channels/<channel-id> {{"account_id": {account_id}}}')


if __name__ == "__main__":
    main()
