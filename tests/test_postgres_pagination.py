"""Stable pagination, proved against the engine that can actually break it.

``tests/test_search_pagination.py`` walks the same property through the
HTTP API on SQLite. That file is worth having and it is *not* proof: with
the tie-breaker deleted it still passes, because SQLite answers a tied
``ORDER BY`` from a rowid scan and happens to return a consistent order.
A guard whose test passes with the guard removed proves nothing about the
guard.

Postgres does break it, and not rarely. For ``ORDER BY <tied column>
LIMIT n OFFSET m`` the planner uses a top-N heapsort, whose output for
tied rows depends on ``n + m`` — so consecutive pages are cut from two
differently-ordered sequences. Measured on PostgreSQL 16, 2000 rows
sharing one ``created_at``, paged 5 at a time:

    ORDER BY created_at DESC              -> 1802 of 2000 rows reachable
    ORDER BY created_at DESC, id DESC     -> 2000 of 2000 rows reachable

198 rows — 9.9% of the collection — were unreachable through paging while
``total`` kept reporting all 2000. No error, no warning, and nothing in
any single response to show a row had been skipped.

**And it is plan-dependent, which is why the tie-breaker is
unconditional.** The same measurement run against a ``created_at`` that
*has* a usable index reaches all 2000 rows without any tie-breaker,
because the planner answers from an ``Index Scan Backward`` instead of a
heapsort and an index scan has one fixed order. So whether this defect is
live depends on which plan the planner picks today — on the indexes that
happen to exist, the statistics, the row count. ``sort="date"`` is served
by ``ix_links_workspace_created`` and does not break; ``sort="domain"``
has no such index and loses 198 rows. Adding an index, dropping one, or a
table simply growing past a cost threshold flips a sort between the two,
with nothing to announce it. Correctness cannot rest on that, so every
sort ends on ``id`` whether or not it currently needs to — and the tests
below run every sort rather than the ones observed to break.

Skipped when PG_TEST_DSN is unset, like the other Postgres-backed file.
CI sets it against a service container.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import normalize_database_url
from app.database import Base
from app.linkquery import SORT_OPTIONS, filtered_links, ordered_links
from app.models import Channel, Link, Workspace

PG_DSN = os.environ.get("PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="PG_TEST_DSN not set; skipping Postgres-backed tests")

# Large enough that the planner picks a top-N heapsort rather than reading
# the whole table — which is the plan that reorders ties. At a handful of
# rows Postgres sorts everything and the defect hides, exactly as it does
# on SQLite.
LINK_COUNT = 2000
PAGE_SIZE = 5

# One timestamp for every row, to the microsecond. Production reaches this
# shape whenever links are written in one flush.
SHARED_TIMESTAMP = datetime(2026, 3, 1, 12, 0, 0, 500000)


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(normalize_database_url(PG_DSN or ""))
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def tied_workspace(pg_engine) -> int:
    """A workspace whose links are indistinguishable to every sort key."""
    with Session(pg_engine) as db:
        workspace = Workspace(name="Paging WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="paging-1", title="src")
        db.add(channel)
        db.flush()
        db.add_all(
            Link(
                workspace_id=workspace.id,
                channel_id=channel.id,
                message_id=i,
                url=f"https://tied.example/{i}",
                url_hash=f"tiedhash-{i:06d}",
                domain="tied.example",
                platform="web",
                category="other",
                confidence=0.5,
                classified_by="rules-v2",
                raw_text="tied",
                created_at=SHARED_TIMESTAMP,
            )
            for i in range(LINK_COUNT)
        )
        db.commit()
        # ANALYZE so the planner costs the sort from real statistics rather
        # than from defaults — the wrong plan would hide the defect.
        db.execute(Link.__table__.select().limit(0))
        db.connection().exec_driver_sql("ANALYZE links")
        db.commit()
        return workspace.id


def _walk(db: Session, workspace_id: int, *, sort: str, page_size: int = PAGE_SIZE) -> list[int]:
    """Every link id reachable by paging, in the order paging returns it."""
    base, _ = filtered_links(db, workspace_id, q=None, category=None)
    total = base.count()

    seen: list[int] = []
    offset = 0
    while offset < total:
        page, _ = filtered_links(db, workspace_id, q=None, category=None)
        seen.extend(row.id for row in ordered_links(page, sort=sort).offset(offset).limit(page_size))
        offset += page_size
    return seen


def test_paging_reaches_every_link_on_postgres(pg_engine, tied_workspace):
    """Nothing is unreachable and nothing is served twice.

    Runs every option in ``SORT_OPTIONS``: a sort added later without a
    unique final key is the regression this file exists to catch, so the
    list is read from the query layer rather than repeated here.
    """
    with Session(pg_engine) as db:
        for sort in SORT_OPTIONS:
            reached = _walk(db, tied_workspace, sort=sort)

            assert len(set(reached)) == LINK_COUNT, (
                f"sort={sort}: paging reached {len(set(reached))} distinct links of {LINK_COUNT}. "
                f"{LINK_COUNT - len(set(reached))} links cannot be seen through the API at all, "
                "while `total` still counts them."
            )
            assert len(reached) == LINK_COUNT, (
                f"sort={sort}: paging returned {len(reached)} rows for {LINK_COUNT} links — "
                "some link was served on more than one page"
            )


def test_the_same_query_pages_identically_twice(pg_engine, tied_workspace):
    """A client refetching page 2 gets the page it got before.

    Stated plainly: this one does *not* catch a missing tie-breaker, and
    it still passes with the guard deleted. Postgres's heapsort is
    deterministic for a fixed ``n``, so re-running an identical query over
    unchanged rows reproduces the same order — the same *wrong* order.
    What it does catch is an ordering that is nondeterministic run to run:
    a parallel plan whose workers finish in a different sequence, or a
    hash-based plan with no final sort. Those break a client's paging just
    as badly and no other test here would notice.
    """
    with Session(pg_engine) as db:
        for sort in SORT_OPTIONS:
            assert _walk(db, tied_workspace, sort=sort) == _walk(db, tied_workspace, sort=sort), (
                f"sort={sort}: the same unchanged rows came back in a different order on a second run"
            )


def test_page_size_does_not_change_which_links_are_reachable(pg_engine, tied_workspace):
    """The order is the query's property, not the slice's.

    The top-N heapsort's output depends on how many rows are asked for, so
    reading the set in pages of 5 and in pages of 500 produces two
    different orders — and two different *sets*.

    ``domain`` rather than ``date``: the date sort is served by
    ``ix_links_workspace_created``, so Postgres answers it from an index
    scan whose order is fixed regardless of the tie-breaker, and this
    assertion would hold with the guard deleted. ``domain`` has no index
    that satisfies its ordering, so it takes the heapsort path where the
    tie-breaker is the only thing keeping the pages aligned.
    """
    with Session(pg_engine) as db:
        small = _walk(db, tied_workspace, sort="domain", page_size=PAGE_SIZE)
        large = _walk(db, tied_workspace, sort="domain", page_size=500)

        assert small == large, "the result order depends on page size, so page boundaries are not stable"
