"""a domain on every classification correction

Revision ID: 0018_feedback_domain
Revises: 0017_workflow_runs
Create Date: 2026-08-21

Idea 163 asks "is this domain corrected over and over?", which is a
GROUP BY on a column that did not exist — the table stored the full URL,
and two corrections on the same site are two different URLs. Deriving the
domain in Python at query time would mean loading every correction row on
every correction, so the grouping key is stored and indexed instead.
"""

from __future__ import annotations

from urllib.parse import urlparse

import sqlalchemy as sa

from alembic import op

revision = "0018_feedback_domain"
down_revision = "0017_workflow_runs"
branch_labels = None
depends_on = None


def _domain_of(url: str) -> str:
    """A frozen copy of app.ingest.domain_of, deliberately not imported.

    A migration describes what the schema looked like at one moment. If it
    imported the live helper, a later change to how domains are parsed
    would silently change what this already-applied migration *did*, and
    re-running it on a fresh database would produce different rows than it
    produced on an old one.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return url[:300]
    return (netloc[4:] if netloc.startswith("www.") else netloc) or url[:300]


def upgrade() -> None:
    op.add_column(
        "classification_feedback",
        sa.Column("domain", sa.String(length=300), nullable=False, server_default=""),
    )

    # Backfill row by row rather than in SQL: extracting a host from a URL
    # is not something SQLite and Postgres both do the same way, and the
    # table holds one row per human correction — it is small by nature.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, url FROM classification_feedback")).fetchall()
    for row_id, url in rows:
        bind.execute(
            sa.text("UPDATE classification_feedback SET domain = :domain WHERE id = :id"),
            {"domain": _domain_of(url or ""), "id": row_id},
        )

    op.create_index("ix_feedback_workspace_domain", "classification_feedback", ["workspace_id", "domain"])


def downgrade() -> None:
    op.drop_index("ix_feedback_workspace_domain", table_name="classification_feedback")
    op.drop_column("classification_feedback", "domain")
