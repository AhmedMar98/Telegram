"""
Classify worker — picks NEW links and runs them through the classification pipeline.

Per-link flow:
    1. Lock via Redis (prevent duplicate work across workers)
    2. Run ClassificationPipeline.classify()
    3. Apply result to Link + write ClassificationLog
    4. Compute embedding for semantic memory + search
    5. Release lock
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from ..classifier.pipeline import ClassificationPipeline
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

    pipeline = ClassificationPipeline(llm_orchestrator=orchestrator)
    classified = 0

    for link in links:
        with SessionLocal() as db:
            fresh = db.get(Link, link.id)
            if fresh is None:
                continue
            try:
                result = await pipeline.classify(fresh.url, context=fresh.title or "")
                pipeline.db = db  # type: ignore
                pipeline.apply_to_link(fresh, result)
                db.commit()
                classified += 1
                logger.info("[classify] #{} → {} ({}, {})",
                            fresh.id, result.category.value,
                            result.tier.value, result.confidence)

                # Embedding for semantic search
                if orchestrator is not None:
                    text = " ".join(filter(None, [fresh.url, fresh.title or "", fresh.description or ""]))
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
