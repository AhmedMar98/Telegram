"""the number in a bot result line has to mean something

Revision ID: 0022_bot_result_context
Revises: 0021_dialog_kinds
Create Date: 2026-09-01

``answer_results`` numbers each line by its position within the active
filter and page — "3." on page 2 of a search for "vpn" is the eighth
matching link. ``/details 3`` then re-queried with no filter and no page
and answered with the third-newest link in the workspace. The two numbers
agreed only when the user had run no search and stayed on page one, which
is why it looked like it worked.

The same gap broke paging on its own. A Telegram callback arrives with no
memory of what produced it, so the filter rode inside ``callback_data`` —
capped at 64 bytes, which forced the search term to be truncated to 30
characters. Page 2 of a longer search was therefore a page of a
*different* search, silently.

Both are the same missing thing: nowhere to keep what the chat is
currently looking at. These three columns are that place.

The page is deliberately not among them. The number printed beside a
result is absolute within the filter — page 2 prints 6 to 10 — so a
position resolves without it, and a stored page would be a second source
of truth for something already on the wire in the pager's callback.

All three are nullable because "has not searched yet" is a real state,
not a missing value: it means the unfiltered list, which is what
``/latest`` shows.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_bot_result_context"
down_revision = "0021_dialog_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_links", sa.Column("last_query", sa.String(length=255), nullable=True))
    op.add_column("bot_links", sa.Column("last_category", sa.String(length=50), nullable=True))
    op.add_column("bot_links", sa.Column("last_favorite", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("bot_links", "last_favorite")
    op.drop_column("bot_links", "last_category")
    op.drop_column("bot_links", "last_query")
