"""Re-encrypt stored secrets under a new FIELD_ENCRYPTION_KEY.

Without this, rotating the key is a one-way door: every
``TelegramAccount.session_string`` was encrypted with the old key, so the
collector starts failing to decrypt them and the only recovery is
re-registering every account by hand. ``docs/02-core-idea.md`` names that
gap explicitly ("no automatic rotation, no KMS") — this closes the
rotation half of it.

    FIELD_ENCRYPTION_KEY_OLD="<current key>" \
    FIELD_ENCRYPTION_KEY="<new key>" \
      python scripts/rotate_encryption_key.py --dry-run

Run with ``--dry-run`` first: it reports exactly what would change and
touches nothing. Then re-run without it to write.

Safety properties, in the order they matter:

- **Idempotent.** A row already readable under the *new* key is counted as
  ``already_rotated`` and left alone, so an interrupted run can simply be
  run again. This is what makes the operation safe to retry rather than a
  gamble on it completing.
- **All-or-nothing.** Every row is decrypted and re-encrypted in memory
  first; the transaction commits only if all of them succeeded. A single
  undecryptable row aborts the whole run rather than leaving the table
  split across two keys.
- **Never logs a secret.** Only counts and row ids are printed.

Required environment:
  DATABASE_URL              - the database to rewrite
  FIELD_ENCRYPTION_KEY_OLD  - the key the rows are encrypted with now
  FIELD_ENCRYPTION_KEY      - the key to re-encrypt them under
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import TelegramAccount  # noqa: E402


@dataclass
class RotationSummary:
    rotated: int = 0
    already_rotated: int = 0
    total: int = 0

    def describe(self) -> str:
        return (
            f"{self.total} account(s): {self.rotated} to re-encrypt, "
            f"{self.already_rotated} already under the new key"
        )


def rotate(old_key: str, new_key: str, *, dry_run: bool) -> RotationSummary:
    """Re-encrypt every account's session string from ``old_key`` to ``new_key``."""
    old = Fernet(old_key.encode("utf-8"))
    new = Fernet(new_key.encode("utf-8"))

    summary = RotationSummary()
    db = SessionLocal()
    try:
        accounts = db.query(TelegramAccount).order_by(TelegramAccount.id).all()
        summary.total = len(accounts)

        pending: list[tuple[TelegramAccount, str]] = []
        for account in accounts:
            stored = account.session_string.encode("utf-8")
            try:
                plaintext = old.decrypt(stored)
            except InvalidToken:
                # Not readable with the old key. Either it is already under
                # the new one (a resumed run) or it is genuinely broken —
                # and those two need very different responses.
                try:
                    new.decrypt(stored)
                except InvalidToken:
                    raise SystemExit(
                        f"account id={account.id} ({account.label!r}) cannot be decrypted with "
                        "either key. Refusing to continue: rotating now would leave the table "
                        "split across keys. Check FIELD_ENCRYPTION_KEY_OLD is correct."
                    ) from None
                summary.already_rotated += 1
                continue
            pending.append((account, new.encrypt(plaintext).decode("utf-8")))

        summary.rotated = len(pending)

        if dry_run:
            return summary

        # Only reached when every row above decrypted cleanly.
        for account, reencrypted in pending:
            account.session_string = reencrypted
        db.commit()
        return summary
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing anything")
    args = parser.parse_args()

    old_key = os.environ.get("FIELD_ENCRYPTION_KEY_OLD", "").strip()
    new_key = os.environ.get("FIELD_ENCRYPTION_KEY", "").strip()
    if not old_key:
        raise SystemExit("FIELD_ENCRYPTION_KEY_OLD is not set (the key the rows use today)")
    if not new_key:
        raise SystemExit("FIELD_ENCRYPTION_KEY is not set (the key to re-encrypt under)")
    if old_key == new_key:
        raise SystemExit("FIELD_ENCRYPTION_KEY_OLD and FIELD_ENCRYPTION_KEY are identical; nothing to do")

    summary = rotate(old_key, new_key, dry_run=args.dry_run)
    prefix = "[dry-run] would rotate" if args.dry_run else "rotated"
    print(f"{prefix}: {summary.describe()}")
    if args.dry_run and summary.rotated:
        print("Re-run without --dry-run to apply. Update FIELD_ENCRYPTION_KEY everywhere afterwards.")


if __name__ == "__main__":
    main()
