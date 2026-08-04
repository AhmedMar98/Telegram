"""
Semantic Memory — reuses classification results when a similar URL was already seen.

Flow:
    1. Before classifying a new link, compute its embedding (384-dim MiniLM).
    2. Search semantic_cache for embeddings with cosine similarity > threshold (default 0.92).
    3. If hit → reuse category, tier, confidence; mark semantic_reused=True.
    4. If miss → run full pipeline, then store the result in cache.

This saves ~4% of LLM calls (the tier that benefits most is when similar
Telegram invite links are repeatedly collected across channels).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from loguru import logger
from sqlalchemy import select

from ..database import SessionLocal
from ..models import SemanticCache


@dataclass
class SemanticHit:
    """A semantic memory cache hit."""
    url: str
    category: str
    tier: str
    confidence: float
    similarity: float
    cache_id: int


class SemanticMemory:
    """Vector lookup cache for classification reuse."""

    def __init__(self, threshold: float = 0.92) -> None:
        self.threshold = threshold

    def find_similar(
        self, embedding: List[float], top_k: int = 5
    ) -> Optional[SemanticHit]:
        """
        Search the cache for the most similar embedding above the threshold.

        Uses pure-Python cosine similarity (no sqlite-vec dependency required
        for the cache scan — works even when sqlite-vec extension isn't loaded).
        """
        if not embedding:
            return None

        with SessionLocal() as db:
            rows = db.execute(
                select(SemanticCache.id, SemanticCache.url, SemanticCache.embedding_json,
                       SemanticCache.category, SemanticCache.tier, SemanticCache.confidence)
                .order_by(SemanticCache.last_used_at.desc().nullsfirst())
                .limit(500)
            ).all()

        if not rows:
            return None

        best: Optional[SemanticHit] = None
        best_sim = 0.0
        for row in rows:
            try:
                cached_vec = json.loads(row.embedding_json)
            except (json.JSONDecodeError, TypeError):
                continue
            sim = _cosine(embedding, cached_vec)
            if sim > best_sim:
                best_sim = sim
                best = SemanticHit(
                    url=row.url,
                    category=row.category,
                    tier=row.tier,
                    confidence=row.confidence,
                    similarity=sim,
                    cache_id=row.id,
                )

        if best is not None and best.similarity >= self.threshold:
            logger.debug(
                "[semantic-memory] HIT: sim={:.4f} (≥{}) url={} → {}",
                best.similarity, self.threshold, best.url[:60], best.category,
            )
            self._mark_used(best.cache_id)
            return best

        logger.debug("[semantic-memory] MISS (best sim={:.4f})", best_sim)
        return None

    def store(
        self,
        url: str,
        url_hash: str,
        embedding: List[float],
        category: str,
        tier: str,
        confidence: float,
    ) -> None:
        """Store a new classification result in the cache."""
        if not embedding:
            return
        with SessionLocal() as db:
            existing = db.query(SemanticCache).filter(
                SemanticCache.url_hash == url_hash
            ).first()
            if existing:
                # Update embedding in case the URL's surrounding text changed
                existing.embedding_json = json.dumps(embedding)
                existing.category = category
                existing.tier = tier
                existing.confidence = confidence
                db.commit()
                return
            db.add(SemanticCache(
                url_hash=url_hash,
                url=url,
                embedding_json=json.dumps(embedding),
                category=category,
                tier=tier,
                confidence=confidence,
            ))
            db.commit()

    def stats(self) -> dict:
        """Return cache statistics for /metrics endpoint."""
        with SessionLocal() as db:
            total = db.query(SemanticCache).count()
            rows = db.query(SemanticCache.used_count).all()
            total_reused = sum(r[0] or 0 for r in rows)
        return {
            "cache_entries": total,
            "total_reuses": total_reused,
            "avg_reuse_per_entry": (total_reused / total) if total > 0 else 0.0,
        }

    def _mark_used(self, cache_id: int) -> None:
        """Increment used_count + update last_used_at."""
        try:
            with SessionLocal() as db:
                row = db.get(SemanticCache, cache_id)
                if row:
                    row.used_count = (row.used_count or 0) + 1
                    row.last_used_at = datetime.utcnow()
                    db.commit()
        except Exception as e:
            logger.debug("[semantic-memory] mark_used failed: {}", e)


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity (0..1 for normalized vectors, can go to -1)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
