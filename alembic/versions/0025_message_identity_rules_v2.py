"""message identity, canonical dedupe keys, and rules-v2 categories

Revision ID: 0025_message_identity_rules_v2
Revises: 0024_leads_and_beneficiaries
Create Date: 2026-09-01

Four changes, one migration, because three of them touch the same table
scan and splitting them would mean walking ``links`` three times on a free
tier that has one shared CPU.

``messages``
    The identity a link could never carry. ``links`` is unique per
    ``(channel_id, url_hash)``, which answers "do I have this URL from this
    channel?" and cannot answer "have I already read this message?" —
    the question both readers (hourly collector, live listener) need when
    they overlap, and the only question a message with no links at all can
    be asked. Deliberately without a ``text`` column: see §43.5.

``links.message_ref_id``
    Nullable forever. Manual entry and imports have no Telegram message
    behind them, and every row that predates this migration has none
    either; NULL is a true statement about origin, not missing data.

``links.url_hash`` recomputed
    The key now covers the *canonical* URL, so the same link written with
    ``?utm_source=``, a ``www.`` prefix or a trailing slash stops producing
    a second row. **This merges rows that already exist**, and merging is
    destructive: where two rows in one channel now share a key, the oldest
    is kept and the rest are deleted. That is the correct resolution — they
    were always the same link — but it cannot be undone by ``downgrade``,
    which is stated here rather than discovered.

``links.category`` recomputed
    Existing rows are re-classified through ``rules-v2``. Not doing this
    was the tempting option and the wrong one: the new classifier would
    apply to new links only, every stored row would keep a category the
    current rules would never produce, and the feature would look finished
    while the data stayed wrong — the same trap as the dialog-kind upgrade
    in §39. Rows a human corrected (``classified_by = 'manual'``) are never
    touched: a human correction outranks every rule.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# Thirty characters, and the limit is thirty-two: ``alembic_version``
# stores this in a ``VARCHAR(32)``. A longer id applies every schema change
# and then fails on the *stamp*, rolling the whole migration back — and
# SQLite, which ignores declared string lengths, reports success. Measured
# here on PostgreSQL 16 with an earlier 35-character id.
revision = "0025_message_identity_rules_v2"
down_revision = "0024_leads_and_beneficiaries"
branch_labels = None
depends_on = None

BATCH = 2000

# Copied rather than imported from app.rls, for the reason spelled out in
# 0020_row_level_security.py: a migration describes the schema at one
# moment, and importing a live list would let a later edit change what an
# already-applied migration did.
POLICY = "tenant_isolation"
SETTING = "app.workspace_id"


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("tg_message_id", sa.Integer(), nullable=False),
        sa.Column("sender_id", sa.String(length=64), nullable=True),
        sa.Column("sender_username", sa.String(length=200), nullable=True),
        sa.Column("sender_name", sa.String(length=300), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("channel_id", "tg_message_id", name="uq_message_per_channel"),
    )
    op.create_index("ix_messages_workspace_id", "messages", ["workspace_id"])
    op.create_index("ix_messages_channel_id", "messages", ["channel_id"])
    op.create_index("ix_messages_sender_id", "messages", ["sender_id"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Same shape as every other protected table. FORCE is not optional:
        # without it the owning role — which is the role this application
        # connects as — bypasses the policy entirely.
        op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON messages "
            f"USING (workspace_id = NULLIF(current_setting('{SETTING}', true), '')::int)"
        )

    op.add_column("links", sa.Column("message_ref_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=True))
    op.create_index("ix_links_message_ref_id", "links", ["message_ref_id"])

    _recompute_links()

    _drop_groq_preferences()


def _drop_groq_preferences() -> None:
    """Remove preference rows for the alert type that no longer exists.

    Preferences are settings, so a row for a setting nobody can set is dead
    weight. The *history* in ``notifications`` is deliberately left alone:
    an alert that really was raised in the past stays true after the
    feature is gone.

    **The FORCE toggle is the point of this function.** ``DELETE FROM
    notification_preferences`` from a migration matches *nothing*: the
    table has forced row-level security, this connection carries no tenant,
    and the policy fails closed — so the statement reports success and
    removes zero rows. Measured on PostgreSQL 16, where the row survived a
    migration that claimed to have deleted it.

    A migration is a schema operation, not a request, so lifting FORCE for
    the length of one statement is the right instrument rather than a
    loophole. It is safe against a mid-migration failure because Alembic
    runs this in a single transaction on Postgres: a rollback takes the
    ``NO FORCE`` with it. The re-enable is asserted rather than assumed.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.execute("DELETE FROM notification_preferences WHERE alert_type = 'groq_quota'")
        return

    op.execute("ALTER TABLE notification_preferences NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM notification_preferences WHERE alert_type = 'groq_quota'")
    op.execute("ALTER TABLE notification_preferences FORCE ROW LEVEL SECURITY")

    forced = bind.execute(
        sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'notification_preferences'")
    ).scalar()
    if not forced:
        raise RuntimeError(
            "0025 left notification_preferences without FORCE ROW LEVEL SECURITY — "
            "refusing to finish a migration that would leave the table readable across tenants"
        )


def _recompute_links() -> None:
    """Re-hash and re-classify every link, oldest first, in batches.

    Imported inside the function rather than at module scope for the reason
    given in 0023: a migration that cannot import because application code
    moved is a migration that cannot run on an old database, which is the
    one job it has.
    """
    from app.classifier import classify_link, hash_url

    bind = op.get_bind()
    links = sa.table(
        "links",
        sa.column("id", sa.Integer),
        sa.column("channel_id", sa.Integer),
        sa.column("url", sa.Text),
        sa.column("url_hash", sa.String),
        sa.column("raw_text", sa.Text),
        sa.column("category", sa.String),
        sa.column("confidence", sa.Float),
        sa.column("classified_by", sa.String),
        sa.column("matched_rule", sa.String),
    )

    # (channel_id, url_hash) pairs already committed by an earlier batch.
    # Needed because the merge is decided *across* batches too: the same
    # link can appear at row 10 and row 90,000.
    seen: set[tuple[int, str]] = set()
    doomed: list[int] = []
    last_id = 0

    while True:
        rows = bind.execute(
            sa.select(
                links.c.id,
                links.c.channel_id,
                links.c.url,
                links.c.raw_text,
                links.c.classified_by,
            )
            .where(links.c.id > last_id)
            .order_by(links.c.id)
            .limit(BATCH)
        ).fetchall()
        if not rows:
            break

        for row_id, channel_id, url, raw_text, classified_by in rows:
            key = (channel_id, hash_url(url or ""))
            if key in seen:
                # A duplicate of a row already kept. Ordered by id, so the
                # survivor is always the oldest — the one whose created_at,
                # click count and any human correction are real history.
                doomed.append(row_id)
                continue
            seen.add(key)

            values: dict[str, object] = {"url_hash": key[1]}
            if classified_by != "manual":
                result = classify_link(url or "", raw_text or "")
                values.update(
                    category=result.category,
                    confidence=result.confidence,
                    classified_by="rules-v2",
                    matched_rule=result.matched_rule[:100],
                )
            bind.execute(links.update().where(links.c.id == row_id).values(**values))

        last_id = rows[-1][0]

    if doomed:
        # After the loop, not during: deleting while paginating on id is
        # safe here (the cursor only moves forward) but doing it in one
        # statement keeps the row count in the log truthful.
        for start in range(0, len(doomed), BATCH):
            chunk = doomed[start : start + BATCH]
            bind.execute(links.delete().where(links.c.id.in_(chunk)))


def downgrade() -> None:
    """Reverse the schema, and the hash, as far as it can be reversed.

    **The merged duplicate rows do not come back.** They were deleted
    because they were the same link twice; nothing records what was
    removed, and inventing rows to restore would be worse than the honest
    gap. Everything else is restored: the old lower-cased hash, the old
    ``classified_by`` values, and the schema.

    Categories are *not* re-derived by the v1 rules: that code no longer
    exists, and a downgrade that guesses at what a deleted classifier would
    have said produces data that never existed in either version.
    """
    bind = op.get_bind()
    links = sa.table(
        "links",
        sa.column("id", sa.Integer),
        sa.column("url", sa.Text),
        sa.column("url_hash", sa.String),
        sa.column("classified_by", sa.String),
    )

    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(links.c.id, links.c.url).where(links.c.id > last_id).order_by(links.c.id).limit(BATCH)
        ).fetchall()
        if not rows:
            break
        for row_id, url in rows:
            import hashlib

            old_hash = hashlib.sha256((url or "").strip().lower().encode("utf-8")).hexdigest()
            bind.execute(links.update().where(links.c.id == row_id).values(url_hash=old_hash))
        last_id = rows[-1][0]

    bind.execute(links.update().where(links.c.classified_by == "rules-v2").values(classified_by="rules"))

    op.drop_index("ix_links_message_ref_id", table_name="links")
    op.drop_column("links", "message_ref_id")

    if bind.dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON messages")
    op.drop_index("ix_messages_sender_id", table_name="messages")
    op.drop_index("ix_messages_channel_id", table_name="messages")
    op.drop_index("ix_messages_workspace_id", table_name="messages")
    op.drop_table("messages")
