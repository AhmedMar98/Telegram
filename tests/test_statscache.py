"""The stats cache: does it hit, does it expire, and does a write clear it?

Every test here exists because of a specific way this could be wrong
while looking right. A cache that never hits is invisible — the endpoint
still returns correct data, just slowly, which is precisely the bug the
cache was added to fix. So the first test counts queries rather than
trusting that a second call "felt" cached.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from app import statscache
from app.database import engine
from tests.conftest import register_workspace


@pytest.fixture(autouse=True)
def _empty_cache():
    """Every test starts cold. Otherwise the first test to run populates
    the cache and every later one measures its leftovers."""
    statscache.clear()
    yield
    statscache.clear()


class QueryCounter:
    """Counts SQL statements actually issued, per engine.

    A timing assertion would be flaky on shared CI hardware. A query count
    is exact: a cache hit issues the authentication queries and nothing
    else, and that difference is not a matter of degree.
    """

    def __init__(self, engine):
        self.engine = engine
        self.count = 0

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._on_execute)
        return False


# --- the unit, without any HTTP ------------------------------------------


def test_a_value_comes_back_before_the_ttl():
    statscache.put(7, "payload", now=1000.0)
    assert statscache.get(7, ttl=30, now=1020.0) == "payload"


def test_a_value_is_gone_after_the_ttl():
    statscache.put(7, "payload", now=1000.0)
    assert statscache.get(7, ttl=30, now=1031.0) is None


def test_an_expired_entry_is_dropped_not_merely_hidden():
    """Otherwise an abandoned workspace's payload sits in memory forever,
    on a 512 MB tier where that is not free."""
    statscache.put(7, "payload", now=1000.0)
    statscache.get(7, ttl=30, now=1031.0)
    assert statscache.size() == 0


def test_workspaces_do_not_share_an_entry():
    """The failure this prevents is a cross-tenant data leak, not a stale
    number: workspace 2 being served workspace 1's totals."""
    statscache.put(1, "one", now=1000.0)
    statscache.put(2, "two", now=1000.0)
    assert statscache.get(1, ttl=30, now=1001.0) == "one"
    assert statscache.get(2, ttl=30, now=1001.0) == "two"


def test_a_zero_ttl_disables_the_cache_entirely():
    statscache.put(7, "payload", now=1000.0)
    assert statscache.get(7, ttl=0, now=1000.0) is None


def test_invalidate_removes_only_the_named_workspace():
    statscache.put(1, "one", now=1000.0)
    statscache.put(2, "two", now=1000.0)
    statscache.invalidate(1)
    assert statscache.get(1, ttl=30, now=1001.0) is None
    assert statscache.get(2, ttl=30, now=1001.0) == "two"


# --- through the real endpoint -------------------------------------------


def test_the_second_request_does_not_requery_the_database(client):
    """The test that would fail if the cache were never consulted.

    Not a timing assertion — a count. The first call runs the twelve
    aggregates; the second must run none of them.
    """
    register_workspace(client, email="cache1@example.com", workspace_name="cache one")

    first = client.get("/links/stats")
    assert first.status_code == 200

    with QueryCounter(engine) as counter:
        second = client.get("/links/stats")
    assert second.status_code == 200
    assert second.json() == first.json()

    # Authentication still touches the database on every request; the
    # twelve aggregates must not. The bound is deliberately loose about
    # auth and strict about the difference that matters.
    assert counter.count <= 4, f"cache hit still issued {counter.count} queries — it is not being used"


def test_a_write_makes_the_next_read_recompute(client):
    """Guards the after_commit hook in app/database.py.

    Without it a user adds a link and the counter they are looking at
    does not move for up to the whole TTL — the cache being visibly,
    provably wrong rather than merely old.
    """
    register_workspace(client, email="cache2@example.com", workspace_name="cache two")

    before = client.get("/links/stats").json()

    added = client.post("/links", json={"text": "https://cache-test.example/one"})
    assert added.status_code == 201

    with QueryCounter(engine) as counter:
        after = client.get("/links/stats").json()

    assert after["total_links"] == before["total_links"] + 1
    assert counter.count > 4, "the write did not invalidate the cache — stats were served stale"
