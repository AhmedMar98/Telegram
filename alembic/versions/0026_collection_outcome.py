"""per-source collection outcome, for the measurement contract

Revision ID: 0026_collection_outcome
Revises: 0025_message_identity_rules_v2
Create Date: 2026-09-02

``channels.last_collected_at`` means "last **successful** read" — it is
what the rotation ordering in ``_channels_for`` needs, and it is stamped
only when a read completes. That makes one question unanswerable: was this
source attempted and did it fail, or was it never attempted at all? Both
leave an old timestamp, and coverage computed from an ambiguous pair is a
number with no meaning (§46).

Five columns, all nullable or defaulted, no backfill:

``last_attempt_at``       when it was last tried, successfully or not
``last_outcome``          succeeded | failed | skipped
``last_failure_kind``     one of app.coverage.FAILURE_KINDS, NULL on success
``caught_up``             did the last read reach the end, or hit the cap
``watermark_regressions`` times the watermark was asked to move backwards

**Deliberately not backfilled.** Every existing row gets NULL, which reads
as "never attempted under the contract" — and that is true. Inventing
``succeeded`` for rows collected before the contract existed would put
made-up successes into the first coverage figure anyone looks at, which
is the one number that must not be flattering by construction.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_collection_outcome"
down_revision = "0025_message_identity_rules_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("channels", sa.Column("last_outcome", sa.String(length=20), nullable=True))
    op.add_column("channels", sa.Column("last_failure_kind", sa.String(length=32), nullable=True))
    op.add_column("channels", sa.Column("caught_up", sa.Boolean(), nullable=True))
    op.add_column(
        "channels",
        sa.Column("watermark_regressions", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("channels", "watermark_regressions")
    op.drop_column("channels", "caught_up")
    op.drop_column("channels", "last_failure_kind")
    op.drop_column("channels", "last_outcome")
    op.drop_column("channels", "last_attempt_at")
