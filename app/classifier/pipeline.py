"""
Classification pipeline — orchestrates all tiers in order:

    0. Semantic Memory   (instant reuse if similarity > 0.92 — v4.1)
    1. Rules             (~80% — free, instant)
    2. Metadata          (~15% — HTTP fetch, 1-2s)
    3. LLM               (~4%  — for unresolved links)
    4. LLM deep          (~1%  — ambiguous cases)

Each tier logs a ClassificationLog entry. The pipeline stops at the
first tier that returns a confident result (confidence >= threshold).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from loguru import logger

from ..models import (
    ClassificationLog,
    ClassificationTier,
    Link,
    LinkCategory,
    LinkStatus,
)
from .ai_layer import classify_with_llm
from .metadata_layer import fetch_metadata
from .rules_layer import classify_with_rules
from .semantic_memory import SemanticMemory


@dataclass
class ClassificationResult:
    category: LinkCategory
    confidence: float
    tier: ClassificationTier
    rule_matched: str | None = None
    title: str | None = None
    description: str | None = None
    llm_provider: str | None = None
    semantic_reused: bool = False
    semantic_similarity: float = 0.0


# Confidence thresholds for tier escalation
TIER_RULES_MIN = 0.85
TIER_METADATA_MIN = 0.65


class ClassificationPipeline:
    """Orchestrates tiered classification with semantic memory."""

    def __init__(
        self, llm_orchestrator=None, db_session=None, semantic_memory: SemanticMemory | None = None
    ) -> None:
        self.llm = llm_orchestrator
        self.db = db_session
        # Semantic memory is optional but recommended when LLM is configured
        self.memory = semantic_memory or SemanticMemory()

    async def classify(self, url: str, context: str = "") -> ClassificationResult:
        """Run the full pipeline. Returns the first confident hit."""
        url_hash = hashlib.sha256(url.strip().lower().encode()).hexdigest()

        # Tier 0: Semantic Memory — check cache before any LLM/HTTP call
        if self.llm is not None:
            embedding = self.llm.embed(f"{url} {context}")
            if embedding:
                hit = self.memory.find_similar(embedding)
                if hit is not None:
                    logger.info(
                        "[classify] semantic-memory HIT: {} → {} (sim={:.4f})",
                        url[:60],
                        hit.category,
                        hit.similarity,
                    )
                    # Store under this URL hash too (so future exact matches are instant)
                    self.memory.store(
                        url=url,
                        url_hash=url_hash,
                        embedding=embedding,
                        category=hit.category,
                        tier=hit.tier,
                        confidence=hit.confidence,
                    )
                    return ClassificationResult(
                        category=LinkCategory(hit.category),
                        confidence=hit.confidence,
                        tier=ClassificationTier(hit.tier),
                        semantic_reused=True,
                        semantic_similarity=hit.similarity,
                    )

        # Tier 1: Rules
        rule_res = classify_with_rules(url, context)
        if rule_res and rule_res.confidence >= TIER_RULES_MIN:
            logger.debug(
                "[classify] rules hit: {} → {} ({})",
                url[:60],
                rule_res.category.value,
                rule_res.confidence,
            )
            result = ClassificationResult(
                category=rule_res.category,
                confidence=rule_res.confidence,
                tier=ClassificationTier.RULES,
                rule_matched=rule_res.rule_matched,
            )
            self._maybe_store_memory(url, url_hash, context, result)
            return result

        # Tier 2: Metadata (HTTP fetch)
        meta = await fetch_metadata(url)
        if meta and meta.confidence >= TIER_METADATA_MIN:
            logger.debug(
                "[classify] metadata hit: {} → {} ({})",
                url[:60],
                meta.category.value,
                meta.confidence,
            )
            result = ClassificationResult(
                category=meta.category,
                confidence=meta.confidence,
                tier=ClassificationTier.METADATA,
                title=meta.title,
                description=meta.description,
            )
            self._maybe_store_memory(url, url_hash, context, result)
            return result

        # Tier 3: LLM (if orchestrator configured)
        if self.llm is None:
            # No LLM available → fall back to whatever we have
            cat = meta.category if meta else (rule_res.category if rule_res else LinkCategory.OTHER)
            conf = meta.confidence if meta else (rule_res.confidence if rule_res else 0.30)
            result = ClassificationResult(
                category=cat,
                confidence=conf,
                tier=ClassificationTier.METADATA if meta else ClassificationTier.RULES,
                title=meta.title if meta else None,
                description=meta.description if meta else None,
            )
            return result

        ai_res = await classify_with_llm(self.llm, url, context, deep=False)
        if ai_res and ai_res.confidence >= 0.6:
            logger.debug(
                "[classify] LLM hit: {} → {} ({}) via {}",
                url[:60],
                ai_res.category.value,
                ai_res.confidence,
                ai_res.provider,
            )
            result = ClassificationResult(
                category=ai_res.category,
                confidence=ai_res.confidence,
                tier=ClassificationTier.LLM,
                llm_provider=ai_res.provider,
            )
            self._maybe_store_memory(url, url_hash, context, result)
            return result

        # Tier 4: LLM deep (last resort)
        deep_res = await classify_with_llm(self.llm, url, context, deep=True)
        if deep_res:
            logger.debug(
                "[classify] LLM-deep hit: {} → {} ({})",
                url[:60],
                deep_res.category.value,
                deep_res.confidence,
            )
            result = ClassificationResult(
                category=deep_res.category,
                confidence=max(0.5, deep_res.confidence),
                tier=ClassificationTier.LLM_DEEP,
                llm_provider=deep_res.provider,
            )
            self._maybe_store_memory(url, url_hash, context, result)
            return result

        # Ultimate fallback
        return ClassificationResult(
            category=LinkCategory.OTHER,
            confidence=0.30,
            tier=ClassificationTier.RULES,
        )

    def _maybe_store_memory(
        self,
        url: str,
        url_hash: str,
        context: str,
        result: ClassificationResult,
    ) -> None:
        """Store the classification in semantic cache (best-effort)."""
        if self.llm is None or result.semantic_reused:
            return
        try:
            embedding = self.llm.embed(f"{url} {context}")
            if embedding:
                self.memory.store(
                    url=url,
                    url_hash=url_hash,
                    embedding=embedding,
                    category=result.category.value,
                    tier=result.tier.value,
                    confidence=result.confidence,
                )
        except Exception as e:
            logger.debug("[semantic-memory] store failed: {}", e)

    def apply_to_link(self, link: Link, result: ClassificationResult) -> None:
        """Update a Link ORM object with the classification result + write audit log."""
        link.category = result.category.value
        link.classification_tier = result.tier.value
        link.confidence = result.confidence
        link.status = LinkStatus.CLASSIFIED.value
        if result.title and not link.title:
            link.title = result.title
        if result.description and not link.description:
            link.description = result.description

        if self.db is not None:
            log_entry = ClassificationLog(
                link_id=link.id,
                tier=result.tier.value,
                category=result.category.value,
                confidence=result.confidence,
                rule_matched=result.rule_matched,
                llm_provider=result.llm_provider,
                llm_response=None,
                semantic_reused=result.semantic_reused,
            )
            self.db.add(log_entry)
            self.db.flush()
