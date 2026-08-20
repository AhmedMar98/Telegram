"""notes, pins, click counts, and a measured top-domains index

Revision ID: 0012_notes_pins_clicks
Revises: 0011_saved_searches
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_notes_pins_clicks"
down_revision: str | None = "0011_saved_searches"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    # Nullable: a link with no note genuinely has none, and an empty string
    # would be indistinguishable from a note the user cleared.
    op.add_column("links", sa.Column("notes", sa.Text(), nullable=True))

    # NOT NULL with a real default: false and 0 are correct for every
    # existing row, not placeholders. Added nullable, filled, constrained —
    # Postgres cannot add a NOT NULL column without a default to a
    # non-empty table.
    op.add_column("links", sa.Column("is_pinned", sa.Boolean(), nullable=True))
    op.add_column("links", sa.Column("click_count", sa.Integer(), nullable=True))
    op.execute(sa.text("UPDATE links SET is_pinned = false WHERE is_pinned IS NULL"))
    op.execute(sa.text("UPDATE links SET click_count = 0 WHERE click_count IS NULL"))
    with op.batch_alter_table("links") as batch:
        batch.alter_column("is_pinned", existing_type=sa.Boolean(), nullable=False)
        batch.alter_column("click_count", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_links_is_pinned", "links", ["is_pinned"])

    # The column order here is a measurement, not a guess. On 50k rows:
    #   no index                                  10.7ms, 1118 buffers
    #   (workspace_id, domain)                    ignored by the planner
    #   this index, with count(id)                ignored — id is not in it
    #   this index, with count(*)                 1.9ms, 13 buffers
    # The stats query was changed to count(*) in the same commit; without
    # that change this index does nothing at all.
    op.create_index("ix_links_ws_archived_domain", "links", ["workspace_id", "is_archived", "domain"])


def downgrade() -> None:
    op.drop_index("ix_links_ws_archived_domain", table_name="links")
    op.drop_index("ix_links_is_pinned", table_name="links")
    op.drop_column("links", "click_count")
    op.drop_column("links", "is_pinned")
    op.drop_column("links", "notes")
