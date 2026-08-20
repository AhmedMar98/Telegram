"""collector account health and history

Revision ID: 0013_account_health
Revises: 0012_notes_pins_clicks
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_account_health"
down_revision: str | None = "0012_notes_pins_clicks"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    # Nullable and left NULL for existing rows: an account that predates
    # this migration has no recorded success or failure, and "never
    # recorded" is the truth. Backfilling created_at as a success would
    # claim a run that may never have happened.
    op.add_column("telegram_accounts", sa.Column("last_success_at", sa.DateTime(), nullable=True))
    op.add_column("telegram_accounts", sa.Column("last_failure_at", sa.DateTime(), nullable=True))
    op.add_column("telegram_accounts", sa.Column("last_error", sa.String(length=300), nullable=True))
    op.add_column("telegram_accounts", sa.Column("disabled_reason", sa.String(length=300), nullable=True))

    # Counters are NOT NULL at zero, which is correct rather than a
    # placeholder: no failures are recorded yet, and no links are yet
    # attributed. links_collected deliberately does not backfill from the
    # existing links table — attributing history that was never tracked
    # would be inventing it.
    op.add_column("telegram_accounts", sa.Column("consecutive_failures", sa.Integer(), nullable=True))
    op.add_column("telegram_accounts", sa.Column("links_collected", sa.Integer(), nullable=True))
    op.execute(sa.text("UPDATE telegram_accounts SET consecutive_failures = 0 WHERE consecutive_failures IS NULL"))
    op.execute(sa.text("UPDATE telegram_accounts SET links_collected = 0 WHERE links_collected IS NULL"))
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.alter_column("consecutive_failures", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("links_collected", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    for column in (
        "links_collected",
        "consecutive_failures",
        "disabled_reason",
        "last_error",
        "last_failure_at",
        "last_success_at",
    ):
        op.drop_column("telegram_accounts", column)
