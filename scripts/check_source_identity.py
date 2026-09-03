"""Report sources that are one dialog wearing two identities.

    COLLECTOR_WORKSPACE_ID=1 python scripts/check_source_identity.py

``channels`` is unique on ``tg_channel_id`` — the raw string — while the
identity of a Telegram dialog is the *canonical* peer id: Telethon writes
``-1001234567890`` and a person pastes ``1234567890``, and both name the
same channel. Discovery compares canonically (``app.dialogs.existing_channel``)
but ``get_or_create_channel`` — the path manual entry and both importers
use — compares the raw string, so a deployment can hold two rows for one
dialog. Each carries its own watermark and its own links, and neither is
visibly wrong.

This is the check that has to come back clean before ``identity_key`` can
carry a unique constraint. It is deliberately a report rather than a
repair: merging two sources decides which watermark survives and where the
other's links go, and that is a decision with data consequences, not a
migration's to make quietly.

Exit codes:
    0  no collisions — the constraint can be added
    1  collisions found, listed on stdout
    2  the environment is not usable

Required environment:
    DATABASE_URL              same database the web service uses
    COLLECTOR_WORKSPACE_ID    which workspace to inspect
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Channel  # noqa: E402
from app.rls import scope_session_to_workspace  # noqa: E402


def find_collisions(db, workspace_id: int) -> list[tuple[str, int]]:
    """Identity keys held by more than one source row, worst first.

    Split out of ``main`` so it can be tested without a subprocess and an
    environment: a report nobody has proved detects anything is not a
    gate, and this one guards a constraint that has not been added yet.
    """
    return list(
        db.execute(
            select(Channel.identity_key, func.count(Channel.id))
            .where(Channel.workspace_id == workspace_id, Channel.identity_key.is_not(None))
            .group_by(Channel.identity_key)
            .having(func.count(Channel.id) > 1)
            .order_by(func.count(Channel.id).desc(), Channel.identity_key)
        ).all()
    )


def main() -> int:
    raw = os.environ.get("COLLECTOR_WORKSPACE_ID")
    if not raw:
        print("COLLECTOR_WORKSPACE_ID is not set", file=sys.stderr)
        return 2
    try:
        workspace_id = int(raw)
    except ValueError:
        print(f"COLLECTOR_WORKSPACE_ID is not a number: {raw!r}", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        # channels is under row-level security; without this the query
        # returns zero rows and this script reports a clean database it
        # never actually read.
        scope_session_to_workspace(db, workspace_id)

        duplicates = find_collisions(db, workspace_id)

        if not duplicates:
            total = db.execute(
                select(func.count(Channel.id)).where(Channel.workspace_id == workspace_id)
            ).scalar_one()
            print(f"no identity collisions across {total} source(s) in workspace {workspace_id}")
            return 0

        print(f"{len(duplicates)} identity collision(s) in workspace {workspace_id}:\n")
        for identity, count in duplicates:
            rows = db.execute(
                select(Channel.id, Channel.tg_channel_id, Channel.username, Channel.last_message_id)
                .where(Channel.workspace_id == workspace_id, Channel.identity_key == identity)
                .order_by(Channel.id)
            ).all()
            print(f"  identity {identity!r} is held by {count} rows:")
            for row in rows:
                print(
                    f"    channel {row.id}: tg_channel_id={row.tg_channel_id!r} "
                    f"username={row.username!r} watermark={row.last_message_id}"
                )
            print()
        print("Each pair is one dialog stored twice. Decide which row keeps collecting")
        print("before identity_key is made unique; nothing here changes any data.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
