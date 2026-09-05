"""Paging through a result set must not lose or repeat a link.

AC-SR03 asks for a defined tie-breaker so results are stably ordered when
sort values are equal. That reads like a tidiness requirement and is not:
``OFFSET``/``LIMIT`` paginates by *position* in the sort order, and SQL
leaves the position of tied rows undefined. If two links compare equal on
every ordering column, nothing stops the database from placing one of them
at position 2 while answering page 1 and at position 3 while answering
page 2 — so the reader sees that link twice and never sees the link it
displaced. No error is raised. Nothing in the response says a row was
skipped.

Ties are not a rare shape here. ``Link.created_at`` defaults to
``utcnow()`` evaluated per row at flush time, so a message pasted with
several links, an importer writing a batch, or any seeded fixture can put
identical timestamps on a whole group. The ``domain``, ``category`` and
``confidence`` sorts tie far more easily still — a workspace collecting
from one site has one domain for every row it holds.

So each test below sets one sort's key to a single shared value across
every link, which is the worst case that sort can meet, then walks the
whole result set one page at a time and demands the pages partition it
exactly: every link once, none missing, no order that changes between
runs.
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.linkquery import SORT_OPTIONS
from app.models import Link
from tests.conftest import register_workspace

# One timestamp, to the microsecond, shared by every seeded link. Real
# collection produces this whenever rows are written in one flush; here it
# is pinned so the test does not depend on how fast the machine runs.
SHARED_TIMESTAMP = datetime(2026, 3, 1, 12, 0, 0, 500000)

LINK_COUNT = 12
PAGE_SIZE = 5


def _seed_tied_links(workspace_id: int, channel_id: int, count: int = LINK_COUNT) -> None:
    """``count`` links that are indistinguishable to every sort key.

    Same timestamp, same domain, same category, same confidence, same
    (null) check time — so whichever sort is under test, every row is tied
    with every other row and only the tie-breaker can order them.
    """
    db = SessionLocal()
    try:
        for i in range(count):
            url = f"https://tied.example/{i}"
            db.add(
                Link(
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    message_id=i,
                    url=url,
                    url_hash=hash_url(url),
                    domain="tied.example",
                    platform="web",
                    category="other",
                    confidence=0.5,
                    classified_by="rules-v2",
                    raw_text="tied",
                    created_at=SHARED_TIMESTAMP,
                )
            )
        db.commit()
    finally:
        db.close()


def _walk_pages(client: TestClient, *, sort: str, page_size: int = PAGE_SIZE) -> list[int]:
    """Every link id the API hands back, page by page, in order."""
    seen: list[int] = []
    page = 1
    while True:
        body = client.get("/links", params={"sort": sort, "page": page, "page_size": page_size}).json()
        seen.extend(item["id"] for item in body["items"])
        if page * page_size >= body["total"]:
            return seen
        page += 1


def _workspace_with_tied_links(client: TestClient, email: str) -> None:
    register_workspace(client, email=email, workspace_name="Paging Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    channel = client.post("/channels", json={"tg_channel_id": "900", "username": "paging"}).json()
    _seed_tied_links(workspace_id, channel["id"])


def test_every_sort_partitions_the_result_set_exactly(client: TestClient):
    """No link appears on two pages, and none is skipped between them.

    Runs against every option in ``SORT_OPTIONS`` rather than a chosen
    few: a sort added later without a unique final key is exactly the
    regression this file exists to catch, and listing the sorts by hand
    here would let a new one arrive uncovered.
    """
    _workspace_with_tied_links(client, "partition@example.com")

    for sort in SORT_OPTIONS:
        collected = _walk_pages(client, sort=sort)

        assert len(collected) == LINK_COUNT, (
            f"sort={sort} returned {len(collected)} rows across all pages for {LINK_COUNT} links — "
            "paging over an undefined order duplicates some rows and drops others"
        )
        assert len(set(collected)) == LINK_COUNT, (
            f"sort={sort} returned the same link on more than one page: {collected}"
        )


def test_paging_twice_gives_the_same_pages(client: TestClient):
    """The same query, run again over unchanged data, pages identically.

    Distinct from the partition test above: a result set can be partitioned
    correctly on each individual run and still shuffle between runs, which
    is what makes "page 2" mean something different to a client that
    refetches it.
    """
    _workspace_with_tied_links(client, "stable@example.com")

    for sort in SORT_OPTIONS:
        assert _walk_pages(client, sort=sort) == _walk_pages(client, sort=sort), (
            f"sort={sort} ordered the same unchanged rows differently on a second run"
        )


def test_the_page_boundary_does_not_shift_with_page_size(client: TestClient):
    """One page of twelve holds what four pages of three hold, in order.

    The ordering is a property of the query, not of how the results are
    sliced. If the two disagree, the slice — not the sort — is deciding
    which links a reader sees.
    """
    _workspace_with_tied_links(client, "boundary@example.com")

    for sort in SORT_OPTIONS:
        assert _walk_pages(client, sort=sort, page_size=3) == _walk_pages(
            client, sort=sort, page_size=LINK_COUNT
        ), f"sort={sort} returns a different order depending on page size"


def test_the_last_page_is_not_short_of_the_total(client: TestClient):
    """``total`` and the rows actually reachable by paging agree.

    A count that promises more rows than paging can reach is the visible
    symptom of the invisible bug — and the only one a client could notice
    on its own.
    """
    _workspace_with_tied_links(client, "total@example.com")

    body = client.get("/links", params={"page": 1, "page_size": PAGE_SIZE}).json()

    assert body["total"] == LINK_COUNT
    assert len(_walk_pages(client, sort="date")) == body["total"]
