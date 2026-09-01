"""two axes that one column each was hiding

Revision ID: 0023_platform_and_source
Revises: 0022_bot_result_context
Create Date: 2026-09-01

``links.platform``
    Which service a link points at — Telegram, WhatsApp, YouTube, a plain
    web page. ``category`` already answers "what kind of thing is this?",
    and it cannot also answer "which service is it on": a ``t.me`` link
    may be a film or a course, and a course may live on Telegram or on a
    university's own site. One column forces a choice between two
    questions people actually ask, so this is a second, independent axis.

    Backfilled from the URL rather than defaulted. Every existing row has
    a ``url``, the derivation is deterministic, and leaving 50k rows at
    "web" would make the first thing anyone sees after the upgrade a lie.
    Done in batches with an explicit commit per batch so the migration
    does not hold one transaction open across the whole table.

``channels.source``
    Which reader owns this row: ``userbot`` (MTProto, the only reader
    until now) or ``public`` (the t.me web preview, which needs no
    account).

    This column exists to prevent a specific corruption, not to describe
    anything. ``last_message_id`` is a single watermark per row. If a
    userbot and the public scraper both read the same row, whichever
    finishes last moves the watermark past messages the other never read,
    and those messages are skipped **permanently** — a silent hole in the
    archive that no error surfaces. One row, one reader, enforced by
    filtering on this column at both ends.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_platform_and_source"
down_revision = "0022_bot_result_context"
branch_labels = None
depends_on = None

BATCH = 5000


def upgrade() -> None:
    op.add_column("links", sa.Column("platform", sa.String(length=20), nullable=False, server_default="web"))
    op.create_index("ix_links_platform", "links", ["platform"])
    op.add_column("channels", sa.Column("source", sa.String(length=20), nullable=False, server_default="userbot"))

    _backfill_platform()


def _backfill_platform() -> None:
    """Recompute the platform of every existing row from its URL.

    Imported here rather than at module scope: a migration that fails to
    import because application code moved is a migration that cannot run
    on an old database, which is the one job it has.
    """
    from app.classifier.platform import DEFAULT_PLATFORM, link_platform

    bind = op.get_bind()
    links = sa.table(
        "links",
        sa.column("id", sa.Integer),
        sa.column("url", sa.Text),
        sa.column("platform", sa.String),
    )

    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(links.c.id, links.c.url).where(links.c.id > last_id).order_by(links.c.id).limit(BATCH)
        ).fetchall()
        if not rows:
            break

        # Grouped by platform so a batch is a handful of UPDATE ... IN (...)
        # statements rather than one statement per row.
        by_platform: dict[str, list[int]] = {}
        for row_id, url in rows:
            platform = link_platform(url or "")
            if platform != DEFAULT_PLATFORM:  # the server default already covers "web"
                by_platform.setdefault(platform, []).append(row_id)

        for platform, ids in by_platform.items():
            bind.execute(links.update().where(links.c.id.in_(ids)).values(platform=platform))

        last_id = rows[-1][0]


def downgrade() -> None:
    op.drop_column("channels", "source")
    op.drop_index("ix_links_platform", table_name="links")
    op.drop_column("links", "platform")
