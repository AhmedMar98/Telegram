"""distinguish a dead link from an unreachable one

Adds the state the vitality checker needs to stop calling a
rate-limited server's links dead: when a check last *succeeded*, how many
checks have failed in a row, and whether the user has archived the link
out of their default view.

Revision ID: 0010_vitality_maturity
Revises: 0009_classification_transparency
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_vitality_maturity"
down_revision: str | None = "0009_classification_transparency"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    # Nullable and honest: for a link checked before this migration there is
    # no record of when it was last alive, and last_checked_at is not that
    # answer — it is when it was last *probed*, which for a dead link is
    # precisely the wrong date.
    op.add_column("links", sa.Column("last_alive_at", sa.DateTime(), nullable=True))

    # These two are NOT NULL because zero and false are genuinely correct
    # for every existing row, not placeholders: no link has yet recorded a
    # failure streak, and none has been archived because archiving did not
    # exist. Added nullable, filled, then constrained — Postgres cannot add
    # a NOT NULL column without a default to a non-empty table.
    op.add_column("links", sa.Column("consecutive_failures", sa.Integer(), nullable=True))
    op.add_column("links", sa.Column("is_archived", sa.Boolean(), nullable=True))
    op.execute(sa.text("UPDATE links SET consecutive_failures = 0 WHERE consecutive_failures IS NULL"))
    op.execute(sa.text("UPDATE links SET is_archived = false WHERE is_archived IS NULL"))
    with op.batch_alter_table("links") as batch:
        batch.alter_column("consecutive_failures", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("is_archived", existing_type=sa.Boolean(), nullable=False)
    op.create_index("ix_links_is_archived", "links", ["is_archived"])

    # A link already recorded as dead starts its streak at 1 rather than 0.
    # Starting everything at 0 would give every known-dead link a fresh
    # six-hour re-check cadence on the very first run after deploy, which is
    # the opposite of what the backoff exists to prevent.
    op.execute(sa.text("UPDATE links SET consecutive_failures = 1 WHERE is_alive = false"))

    # Symmetrically, a link last seen alive did have a moment when it was
    # alive, and last_checked_at is that moment for exactly these rows.
    op.execute(sa.text("UPDATE links SET last_alive_at = last_checked_at WHERE is_alive = true"))


def downgrade() -> None:
    op.drop_index("ix_links_is_archived", table_name="links")
    op.drop_column("links", "is_archived")
    op.drop_column("links", "consecutive_failures")
    op.drop_column("links", "last_alive_at")
