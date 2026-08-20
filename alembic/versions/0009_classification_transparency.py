"""explain and correct classifications

Adds the provenance the classifier already knew but discarded
(``matched_rule``, ``source_type``, ``forwarded_from``, ``language``) and
the table that records a human overruling it.

Revision ID: 0009_classification_transparency
Revises: 0008_session_origin
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_classification_transparency"
down_revision: str | None = "0008_session_origin"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    # matched_rule / forwarded_from / language stay nullable: rows written
    # before this migration genuinely have no such value, and NULL says
    # "unknown" where a backfilled default would read like a measurement.
    op.add_column("links", sa.Column("matched_rule", sa.String(length=100), nullable=True))
    op.add_column("links", sa.Column("forwarded_from", sa.String(length=300), nullable=True))
    op.add_column("links", sa.Column("language", sa.String(length=10), nullable=True))

    # source_type is the exception and is NOT NULL, because "unknown
    # provenance" is not a state the application needs to handle. Existing
    # rows are backfilled to "text": that is the path essentially all of them
    # took, but it is the best available label for rows that predate the
    # column, not a measurement of each one. Added nullable first, then
    # filled, then constrained, because Postgres cannot add a NOT NULL column
    # without a default to a non-empty table.
    op.add_column("links", sa.Column("source_type", sa.String(length=20), nullable=True))
    op.execute(sa.text("UPDATE links SET source_type = 'text' WHERE source_type IS NULL"))
    with op.batch_alter_table("links") as batch:
        batch.alter_column("source_type", existing_type=sa.String(length=20), nullable=False)
    op.create_index("ix_links_source_type", "links", ["source_type"])

    op.create_table(
        "classification_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        # Deliberately a plain integer, not a foreign key: a correction on a
        # link that was later deleted is the most useful one to learn from,
        # so the row must survive the link.
        sa.Column("link_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("previous_category", sa.String(length=50), nullable=False),
        sa.Column("new_category", sa.String(length=50), nullable=False),
        sa.Column("previous_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("previous_matched_rule", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_classification_feedback_workspace_id", "classification_feedback", ["workspace_id"])
    op.create_index("ix_classification_feedback_link_id", "classification_feedback", ["link_id"])
    op.create_index("ix_feedback_workspace_created", "classification_feedback", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_workspace_created", table_name="classification_feedback")
    op.drop_index("ix_classification_feedback_link_id", table_name="classification_feedback")
    op.drop_index("ix_classification_feedback_workspace_id", table_name="classification_feedback")
    op.drop_table("classification_feedback")
    op.drop_index("ix_links_source_type", table_name="links")
    op.drop_column("links", "source_type")
    op.drop_column("links", "language")
    op.drop_column("links", "forwarded_from")
    op.drop_column("links", "matched_rule")
