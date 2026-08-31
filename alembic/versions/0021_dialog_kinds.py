"""every dialog is collectable, not only manually-added channels

Revision ID: 0021_dialog_kinds
Revises: 0020_row_level_security
Create Date: 2026-08-31

Until now a row in ``channels`` could only arrive one way: somebody typed
it into the dashboard. That made "collect from Telegram" mean "collect
from the handful of channels you remembered to register", and left the
groups and private conversations that carry most links unreachable.

Two columns make automatic discovery possible without changing what a
channel *is*:

``kind``
    Which sort of dialog the row came from — ``channel``, ``group`` or
    ``private``. Needed because the three are not interchangeable to a
    reader (a link from a friend's DM is not a link from a public
    channel) and because collection scope is configured per kind.
    Backfilled to ``channel``: every row that predates this migration was
    typed in by hand as a channel, so that is not a guess.

``last_collected_at``
    When the scheduled collector last read this dialog. With a handful of
    hand-added channels, ordering by id and taking a prefix was fine. With
    automatic discovery an account can hold hundreds of dialogs and the
    per-run cap becomes a starvation risk: ordering by id forever, the
    dialogs past the cap are never read at all. Ordering by "least
    recently collected, never-collected first" turns the cap into a
    rotation instead of a cliff.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_dialog_kinds"
down_revision = "0020_row_level_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default, not just a Python-side default: existing rows need a
    # value at migration time, and SQLite cannot add a NOT NULL column
    # without one.
    op.add_column(
        "channels",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="channel"),
    )
    op.add_column("channels", sa.Column("last_collected_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("channels", "last_collected_at")
    op.drop_column("channels", "kind")
