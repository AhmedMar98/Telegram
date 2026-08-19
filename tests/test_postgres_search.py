"""Search tests that run against a real Postgres, not the SQLite fallback.

Production runs Postgres and takes the ``to_tsvector`` branch of the
search; development and the rest of the suite run SQLite and take the
``ILIKE`` branch. That gap once hid a real defect — searching for part of
a URL returned nothing in production while working locally — so the
production path needs coverage against the real engine.

Skipped automatically when PG_TEST_DSN is not set, so the SQLite suite
still runs anywhere. CI sets it against a Postgres service container.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.config import normalize_database_url
from app.database import Base
from app.models import Channel, Link, Workspace
from app.search import FTS_DOCUMENT_SQL, fts_document, fts_query

PG_DSN = os.environ.get("PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="PG_TEST_DSN not set; skipping Postgres-backed tests")


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(normalize_database_url(PG_DSN or ""))
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_links_fts ON links USING GIN ({FTS_DOCUMENT_SQL})"))
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def seeded(pg_engine):
    with Session(pg_engine) as db:
        workspace = Workspace(name="PG WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="pg-1", title="src")
        db.add(channel)
        db.flush()
        rows = [
            ("https://design-hub.example/photoshop-course.pdf", "دورة تصميم جرافيك", "books_courses"),
            ("https://games.example/racing-game.exe", "لعبة سباق ممتعة", "games"),
            ("https://cdn.example/movie_1080p.mkv", "فيلم اكشن جديد", "movies_series"),
        ]
        for index, (url, raw, category) in enumerate(rows, start=1):
            db.add(
                Link(
                    workspace_id=workspace.id,
                    channel_id=channel.id,
                    message_id=index,
                    url=url,
                    url_hash=f"hash-{index}",
                    domain="example",
                    category=category,
                    confidence=0.9,
                    classified_by="rules",
                    raw_text=raw,
                )
            )
        db.commit()
        return workspace.id


def _search(engine, workspace_id: int, term: str) -> list[str]:
    with Session(engine) as db:
        stmt = (
            select(Link.url)
            .where(Link.workspace_id == workspace_id)
            .where(fts_document(Link.raw_text, Link.url).op("@@")(fts_query(term)))
        )
        return [row[0] for row in db.execute(stmt)]


@pytest.mark.parametrize(
    ("term", "expected_fragment"),
    [
        # The regression this file exists for: a hyphenated fragment of a URL.
        ("photoshop-course", "photoshop-course.pdf"),
        ("racing-game", "racing-game.exe"),
        # Bare words from inside the URL path.
        ("photoshop", "photoshop-course.pdf"),
        ("movie", "movie_1080p.mkv"),
        # Host fragments.
        ("design-hub", "photoshop-course.pdf"),
        ("games.example", "racing-game.exe"),
        # Arabic from the message body must keep working.
        ("جرافيك", "photoshop-course.pdf"),
        ("سباق", "racing-game.exe"),
        ("اكشن", "movie_1080p.mkv"),
    ],
)
def test_search_finds_the_expected_link(pg_engine, seeded, term: str, expected_fragment: str):
    results = _search(pg_engine, seeded, term)
    assert len(results) == 1, f"{term!r} should match exactly one link, got {results}"
    assert expected_fragment in results[0]


def test_unmatched_term_returns_nothing(pg_engine, seeded):
    assert _search(pg_engine, seeded, "nothingmatchesthis") == []


def test_search_is_scoped_to_the_workspace(pg_engine, seeded):
    """Even on the Postgres path, another workspace must see nothing."""
    assert _search(pg_engine, seeded + 9999, "photoshop") == []


def test_planner_uses_the_gin_index(pg_engine, seeded):
    """Proves the query expression matches the indexed one.

    If the two ever drift, Postgres silently falls back to a sequential
    scan — correct results, but the index is dead weight and search
    degrades as the table grows. Asserting the plan catches that.
    """
    with Session(pg_engine) as db:
        db.execute(text("SET enable_seqscan = off"))
        stmt = select(Link.id).where(fts_document(Link.raw_text, Link.url).op("@@")(fts_query("photoshop")))
        # Rendering literals is not possible here (the regconfig argument has
        # no literal renderer), so compile with named placeholders and pass
        # the parameters through instead of inlining them.
        compiled = stmt.compile(dialect=postgresql.dialect(paramstyle="named"))
        plan = "\n".join(row[0] for row in db.execute(text(f"EXPLAIN {compiled}"), dict(compiled.params)))
    assert "ix_links_fts" in plan, f"GIN index not used; planner chose:\n{plan}"
