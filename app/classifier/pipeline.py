"""
Classification pipeline — orchestrates all tiers in order:

    1. Rules             (~80% — free, instant)
    2. Metadata          (~15% — HTTP fetch, 1-2s)
    3. LLM               (~4%  — for unresolved links)
    4. LLM deep          (~1%  — ambiguous cases)

Each tier logs a ClassificationLog entry. The pipeline stops at the
first tier that returns a confident result (confidence >= threshold).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

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


@dataclass
class ClassificationResult:
    category: LinkCategory
    confidence: float
    tier: ClassificationTier
    rule_matched: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    llm_provider: Optional[str] = None
    semantic_reused: bool = False


# Confidence thresholds for tier escalation
TIER_RULES_MIN = 0.85
TIER_METADATA_MIN = 0.65


class ClassificationPipeline:
    """Orchestrates tiered classification."""

    def __init__(self, llm_orchestrator=None, db_session=None) -> None:
        self.llm = llm_orchestrator
        self.db = db_session

    async def classify(self, url: str, context: str = "") -> ClassificationResult:
        """Run the full pipeline. Returns the first confident hit."""
        # Tier 1: Rules
        rule_res = classify_with_rules(url, context)
        if rule_res and rule_res.confidence >= TIER_RULES_MIN:
            logger.debug("[classify] rules hit: {} → {} ({})",
                         url[:60], rule_res.category.value, rule_res.confidence)
            return ClassificationResult(
                category=rule_res.category,
                confidence=rule_res.confidence,
                tier=ClassificationTier.RULES,
                rule_matched=rule_res.rule_matched,
            )

        # Tier 2: Metadata (HTTP fetch)
        meta = await fetch_metadata(url)
        if meta and meta.confidence >= TIER_METADATA_MIN:
            logger.debug("[classify] metadata hit: {} → {} ({})",
                         url[:60], meta.category.value, meta.confidence)
            return ClassificationResult(
                category=meta.category,
                confidence=meta.confidence,
                tier=ClassificationTier.METADATA,
                title=meta.title,
                description=meta.description,
            )

        # Tier 3: LLM (if orchestrator configured)
        if self.llm is None:
            # No LLM available → fall back to whatever we have
            cat = (meta.category if meta else
                   (rule_res.category if rule_res else LinkCategory.OTHER))
            conf = (meta.confidence if meta else
                    (rule_res.confidence if rule_res else 0.30))
            return ClassificationResult(
                category=cat, confidence=conf,
                tier=ClassificationTier.METADATA if meta else ClassificationTier.RULES,
                title=meta.title if meta else None,
                description=meta.description if meta else None,
            )

        ai_res = await classify_with_llm(self.llm, url, context, deep=False)
        if ai_res and ai_res.confidence >= 0.6:
            logger.debug("[classify] LLM hit: {} → {} ({}) via {}",
                         url[:60], ai_res.category.value, ai_res.confidence, ai_res.provider)
            return ClassificationResult(
                category=ai_res.category,
                confidence=ai_res.confidence,
                tier=ClassificationTier.LLM,
                llm_provider=ai_res.provider,
            )

        # Tier 4: LLM deep (last resort)
        deep_res = await classify_with_llm(self.llm, url, context, deep=True)
        if deep_res:
            logger.debug("[classify] LLM-deep hit: {} → {} ({})",
                         url[:60], deep_res.category.value, deep_res.confidence)
            return ClassificationResult(
                category=deep_res.category,
                confidence=max(0.5, deep_res.confidence),
                tier=ClassificationTier.LLM_DEEP,
                llm_provider=deep_res.provider,
            )

        # Ultimate fallback
        return ClassificationResult(
            category=LinkCategory.OTHER,
            confidence=0.30,
            tier=ClassificationTier.RULES,
        )

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
