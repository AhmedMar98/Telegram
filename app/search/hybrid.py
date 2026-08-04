"""
Hybrid search — combines:
    1. Full-text search (SQLite FTS5) over title/description/url
    2. Semantic search (sqlite-vec over MiniLM embeddings)
    3. Recency boost + popularity boost

Score = α·fts_score + β·semantic_score + γ·recency_boost + δ·popularity_boost
(defaults: 0.4 / 0.4 / 0.1 / 0.1)

FTS5 virtual table is created lazily on first search.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from loguru import logger
from sqlalchemy import select, text

from ..database import SessionLocal, engine
from ..models import Link


@dataclass
class SearchResult:
    link_id: int
    url: str
    title: Optional[str]
    description: Optional[str]
    category: str
    status: str
    alive: Optional[bool]
    score: float
    score_breakdown: dict


# Score weights
W_FTS = 0.40
W_SEMANTIC = 0.40
W_RECENCY = 0.10
W_POPULARITY = 0.10


class HybridSearch:
    """Hybrid text + semantic search over links."""

    FTS_TABLE = "links_fts"

    def __init__(self, llm_orchestrator=None) -> None:
        self.llm = llm_orchestrator
        self._fts_initialized = False

    # ============================================================
    # Setup
    # ============================================================

    def init_fts(self) -> None:
        """Create FTS5 virtual table mirroring the links table."""
        if self._fts_initialized:
            return
        with engine.connect() as conn:
            try:
                conn.execute(text(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {self.FTS_TABLE}
                    USING fts5(
                        link_id UNINDEXED,
                        url, title, description, category,
                        content='links',
                        content_rowid='id',
                        tokenize='unicode61'
                    )
                """))
                # Populate existing rows
                conn.execute(text(f"""
                    INSERT INTO {self.FTS_TABLE}(rowid, link_id, url, title, description, category)
                    SELECT id, id, url, COALESCE(title,''), COALESCE(description,''), category
                    FROM links
                    WHERE id NOT IN (SELECT link_id FROM {self.FTS_TABLE})
                """))
                conn.commit()
                self._fts_initialized = True
                logger.info("FTS5 table '{}' initialized", self.FTS_TABLE)
            except Exception as e:
                logger.error("FTS init failed: {}", e)

    # ============================================================
    # Search
    # ============================================================

    async def search(
        self,
        query: str,
        category: Optional[str] = None,
        alive_only: bool = False,
        limit: int = 20,
    ) -> List[SearchResult]:
        """Run hybrid search and return ranked results."""
        if not query.strip():
            return []
        self.init_fts()

        # 1. FTS5 query
        fts_ids = self._fts_search(query, limit=limit * 3)

        # 2. Semantic search
        semantic_ids: dict[int, float] = {}
        if self.llm is not None:
            query_vec = self.llm.embed(query)
            if query_vec:
                semantic_ids = self._semantic_search(query_vec, limit=limit * 3)

        # 3. Merge candidate IDs
        all_ids = set(fts_ids.keys()) | set(semantic_ids.keys())
        if not all_ids:
            return []

        # 4. Fetch full Link rows + compute final score
        with SessionLocal() as db:
            stmt = select(Link).where(
                Link.id.in_(all_ids),
                Link.archived.is_(False),
            )
            if category:
                stmt = stmt.where(Link.category == category)
            if alive_only:
                stmt = stmt.where(Link.alive.is_(True))
            links = {l.id: l for l in db.scalars(stmt)}

        results: List[SearchResult] = []
        now = datetime.utcnow()
        for link_id, link in links.items():
            fts_score = fts_ids.get(link_id, 0.0)
            sem_score = semantic_ids.get(link_id, 0.0)
            recency = self._recency_boost(link.discovered_at, now)
            popularity = self._popularity_boost(link.click_count, link.search_hit_count)
            total = (
                W_FTS * min(1.0, fts_score)
                + W_SEMANTIC * sem_score
                + W_RECENCY * recency
                + W_POPULARITY * popularity
            )
            results.append(SearchResult(
                link_id=link.id,
                url=link.url,
                title=link.title,
                description=link.description,
                category=link.category,
                status=link.status,
                alive=link.alive,
                score=round(total, 4),
                score_breakdown={
                    "fts": round(fts_score, 4),
                    "semantic": round(sem_score, 4),
                    "recency": round(recency, 4),
                    "popularity": round(popularity, 4),
                },
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    # ============================================================
    # Helpers
    # ============================================================

    def _fts_search(self, query: str, limit: int = 50) -> dict[int, float]:
        """Run FTS5 MATCH query; return {link_id: bm25_score_normalized}."""
        # FTS5 BM25 score is NEGATIVE (lower = better). We invert + normalize.
        try:
            with engine.connect() as conn:
                # Sanitize query: wrap terms in quotes to prevent injection
                safe_q = " ".join(f'"{t}"' for t in query.split() if t)
                rows = conn.execute(text(f"""
                    SELECT link_id, bm25({self.FTS_TABLE}) AS score
                    FROM {self.FTS_TABLE}
                    WHERE {self.FTS_TABLE} MATCH :q
                    ORDER BY score
                    LIMIT :limit
                """), {"q": safe_q, "limit": limit}).all()
                if not rows:
                    return {}
                # Convert bm25 (negative, lower=better) → 0-1 (higher=better)
                raw = [(r[0], -r[1]) for r in rows]
                max_score = max(s for _, s in raw) or 1.0
                return {lid: s / max_score for lid, s in raw}
        except Exception as e:
            logger.warning("FTS search failed: {}", e)
            return {}

    def _semantic_search(self, query_vec: List[float], limit: int = 50) -> dict[int, float]:
        """Use sqlite-vec for KNN over link embeddings."""
        try:
            import sqlite_vec  # type: ignore
            import json
            with engine.raw_connection() as raw_conn:
                # Ensure extension loaded
                try:
                    raw_conn.enable_load_extension(True)
                    sqlite_vec.load(raw_conn)
                    raw_conn.enable_load_extension(False)
                except Exception:
                    pass

                # Create a temp vec table for the query vector
                cur = raw_conn.cursor()
                dim = len(query_vec)
                cur.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS tmp_query_vec USING vec0(embedding float[{dim}])")
                cur.execute("DELETE FROM tmp_query_vec")
                cur.execute(
                    "INSERT INTO tmp_query_vec(rowid, embedding) VALUES (1, ?)",
                    [json.dumps(query_vec)],
                )
                # KNN join against link_embeddings
                rows = cur.execute(f"""
                    SELECT le.link_id, vec_distance_cosine(le.embedding, tqv.embedding) AS dist
                    FROM link_embeddings le
                    JOIN tmp_query_vec tqv
                    WHERE le.embedding MATCH ?
                      AND k = :k
                    ORDER BY dist
                """, [json.dumps(query_vec)]).fetchall()
                # Also fall back to JSON-based scan if vec table not set up
                if not rows:
                    rows = self._semantic_search_python(query_vec, limit)
                # Convert distance (0=identical, 2=opposite) → similarity (0-1)
                result = {}
                for lid, dist in rows[:limit]:
                    sim = max(0.0, 1.0 - (dist / 2.0))
                    result[lid] = sim
                return result
        except Exception as e:
            logger.warning("Semantic search failed: {}", e)
            return {}

    def _semantic_search_python(self, query_vec: List[float], limit: int) -> list[tuple[int, float]]:
        """Pure-Python fallback: scan all embeddings and rank by cosine similarity."""
        import json
        results = []
        with SessionLocal() as db:
            from ..models import LinkEmbedding
            rows = db.query(LinkEmbedding).all()
            for row in rows:
                try:
                    vec = json.loads(row.embedding_json)
                    sim = _cosine(query_vec, vec)
                    results.append((row.link_id, 1.0 - sim))  # distance = 1 - cosine
                except Exception:
                    continue
        results.sort(key=lambda x: x[1])
        return results[:limit]

    @staticmethod
    def _recency_boost(discovered_at: datetime, now: datetime) -> float:
        """Newer links get a small boost. 0 (old) → 1 (just now)."""
        if not discovered_at:
            return 0.0
        age_days = (now - discovered_at).total_seconds() / 86400
        if age_days <= 0:
            return 1.0
        if age_days >= 30:
            return 0.0
        return 1.0 - (age_days / 30)

    @staticmethod
    def _popularity_boost(clicks: int, search_hits: int) -> float:
        """Logarithmic boost based on engagement."""
        total = (clicks or 0) + (search_hits or 0)
        if total <= 0:
            return 0.0
        return min(1.0, math.log10(total + 1) / 2.0)


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
