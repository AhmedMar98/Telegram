"""
Classify worker — picks NEW links and runs them through the classification pipeline.

Per-link flow:
    1. Lock via Redis (prevent duplicate work across workers)
    2. Run ClassificationPipeline.classify() (with semantic memory tier)
    3. Apply result to Link + write ClassificationLog
    4. Compute embedding for semantic search (if LLM available)
    5. Record Prometheus metrics
    6. Release lock
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from ..classifier.pipeline import ClassificationPipeline
from ..classifier.semantic_memory import SemanticMemory
from ..database import SessionLocal
from ..llm.orchestrator import LLMOrchestrator
from ..models import Link, LinkEmbedding, LinkStatus


# Max links per cycle
BATCH_SIZE = 50


async def classify_once(orchestrator: LLMOrchestrator) -> int:
    """Classify up to BATCH_SIZE NEW links. Returns count classified."""
    with SessionLocal() as db:
        stmt = (
            select(Link)
            .where(Link.status == LinkStatus.NEW.value)
            .order_by(Link.discovered_at.asc())
            .limit(BATCH_SIZE)
        )
        links = list(db.scalars(stmt))

    if not links:
        return 0

    semantic_memory = SemanticMemory()
    pipeline = ClassificationPipeline(
        llm_orchestrator=orchestrator,
        semantic_memory=semantic_memory,
    )
    classified = 0

    for link in links:
        with SessionLocal() as db:
            fresh = db.get(Link, link.id)
            if fresh is None:
                continue
            try:
                t0 = time.perf_counter()
                result = await pipeline.classify(
                    fresh.url,
                    context=fresh.title or "",
                )
                latency = time.perf_counter() - t0
                pipeline.db = db  # type: ignore
                pipeline.apply_to_link(fresh, result)
                db.commit()
                classified += 1

                # v4.1 metrics
                try:
                    from ..web.metrics import record_classified, record_semantic_hit
                    record_classified(tier=result.tier.value, latency_seconds=latency)
                    if result.semantic_reused:
                        record_semantic_hit(hit=True)
                    elif orchestrator is not None:
                        record_semantic_hit(hit=False)
                except Exception:
                    pass

                logger.info(
                    "[classify] #{} → {} ({}, {}, reuse={})",
                    fresh.id, result.category.value,
                    result.tier.value, round(result.confidence, 2),
                    result.semantic_reused,
                )

                # Embedding for semantic search (separate from cache)
                if orchestrator is not None and not result.semantic_reused:
                    text = " ".join(filter(None, [
                        fresh.url, fresh.title or "", fresh.description or "",
                    ]))
                    vec = orchestrator.embed(text)
                    if vec is not None:
                        db.add(LinkEmbedding(
                            link_id=fresh.id,
                            embedding_json=json.dumps(vec),
                            model_name="all-MiniLM-L6-v2",
                            dim=len(vec),
                        ))
                        db.commit()
            except Exception as e:
                logger.exception("[classify] error on link #{}: {}", fresh.id, e)

    logger.info("[classify] cycle done: {} links classified", classified)
    return classified


async def run_loop(interval_sec: int = 30) -> None:
    """Continuously classify NEW links."""
    logger.info("Classify worker started (interval={}s)", interval_sec)
    orch = LLMOrchestrator()
    available = orch.available_providers()
    logger.info("LLM providers available: {}", available or "none (rules-only mode)")

    while True:
        try:
            await classify_once(orch)
        except Exception as e:
            logger.exception("[classify] loop error: {}", e)
        await asyncio.sleep(interval_sec)
