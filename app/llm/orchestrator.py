"""
LLM Orchestrator — routes requests across multiple providers with failover.

Strategy:
    1. Order providers by priority (cheapest/fastest first).
    2. Skip providers in cooldown (checked via QuotaTracker).
    3. Try each provider; on rate-limit (429) → cooldown + failover.
    4. On other error → log + try next provider.
    5. If all fail, return None (caller falls back to rules).

Embeddings:
    Uses sentence-transformers (all-MiniLM-L6-v2, 384-dim) for local, free
    semantic embeddings. Used by the classifier's semantic memory.
"""
from __future__ import annotations

import time
from typing import List, Optional

from loguru import logger

from .providers import (
    BaseLLMProvider,
    GeminiProvider,
    GroqProvider,
    HuggingFaceProvider,
    LLMResponse,
    OpenRouterProvider,
    ZAIProvider,
    # v5.0 additional providers
    CohereProvider,
    MistralProvider,
    TogetherProvider,
    AnyscaleProvider,
    CloudflareProvider,
    # v5.1 NVIDIA Integrate API
    NVIDIAProvider,
)
from .quota_tracker import QuotaTracker


# Provider priority — cheapest/fastest first
# v5.1: expanded to 11 providers (NVIDIA added)
DEFAULT_PRIORITY = [
    "groq",        # LPU, fastest
    "gemini",      # generous free tier
    "cloudflare",  # edge network, fast
    "nvidia",      # NVIDIA Integrate API (glm-5.2, deepseek-v4-pro, etc.)
    "openrouter",  # many free models
    "mistral",     # direct API
    "together",    # open-source models
    "anyscale",    # Llama fine-tunes
    "cohere",      # Command R
    "huggingface", # slower but free
    "zai",         # fallback
]


class LLMOrchestrator:
    """Multi-provider router with failover + quota tracking."""

    def __init__(self, providers: Optional[List[BaseLLMProvider]] = None) -> None:
        if providers is None:
            providers = self._default_providers()
        self.providers: dict[str, BaseLLMProvider] = {p.name: p for p in providers}
        self.priority = [n for n in DEFAULT_PRIORITY if n in self.providers]
        self.tracker = QuotaTracker()
        # Lazy-loaded embedding model
        self._embedder = None
        self._embed_dim: Optional[int] = None

    @staticmethod
    def _default_providers() -> List[BaseLLMProvider]:
        return [
            GroqProvider(),
            GeminiProvider(),
            CloudflareProvider(),
            NVIDIAProvider(),
            OpenRouterProvider(),
            MistralProvider(),
            TogetherProvider(),
            AnyscaleProvider(),
            CohereProvider(),
            HuggingFaceProvider(),
            ZAIProvider(),
        ]

    def available_providers(self) -> List[str]:
        """List providers with credentials configured AND not in cooldown."""
        return [
            name for name in self.priority
            if self.providers[name].is_available and not self.tracker.is_in_cooldown(name)
        ]

    async def chat(
        self,
        messages: List[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> Optional[LLMResponse]:
        """Try each provider in priority order until one succeeds."""
        candidates = self.available_providers()
        if not candidates:
            logger.warning("[orchestrator] no available LLM providers")
            return None

        last_error: Optional[str] = None
        for name in candidates:
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
                response = await provider.chat(messages, max_tokens, temperature)
            except Exception as e:
                logger.warning("[orchestrator] {} raised: {}", name, e)
                self.tracker.update_failure(name, str(e))
                last_error = str(e)
                continue

            latency_ms = int((time.perf_counter() - t0) * 1000)
            if response is None:
                self.tracker.update_failure(name, "no_response")
                last_error = "no_response"
                continue

            response.latency_ms = latency_ms

            # Rate limited?
            if response.error == "rate_limited" or "429" in (response.error or ""):
                self.tracker.update_rate_limited(name, response.quota)
                logger.info("[orchestrator] {} rate-limited, failover", name)
                try:
                    from ..web.metrics import record_llm_call
                    record_llm_call(provider=name, status="rate_limited")
                except Exception:
                    pass
                continue

            if response.error:
                self.tracker.update_failure(name, response.error)
                last_error = response.error
                try:
                    from ..web.metrics import record_llm_call
                    record_llm_call(provider=name, status="error")
                except Exception:
                    pass
                continue

            # Success
            self.tracker.update_success(name, response.quota)
            logger.debug("[orchestrator] {} ok ({}ms, {} in / {} out)",
                         name, latency_ms, response.tokens_in, response.tokens_out)
            # v4.1 Prometheus metrics
            try:
                from ..web.metrics import record_llm_call
                record_llm_call(provider=name, status="success",
                                latency_seconds=latency_ms / 1000.0)
            except Exception:
                pass
            return response

        logger.warning("[orchestrator] all providers failed; last_error={}", last_error)
        return None

    # ============================================================
    # Embeddings (for semantic search & semantic memory)
    # ============================================================

    def _ensure_embedder(self):
        """Lazily load sentence-transformers model."""
        if self._embedder is not None:
            return self._embedder
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            logger.info("Loading embedding model: all-MiniLM-L6-v2")
            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            self._embed_dim = self._embedder.get_sentence_embedding_dimension()
        except ImportError:
            logger.warning("sentence-transformers not installed; embeddings disabled")
            self._embedder = False  # mark as unavailable
        except Exception as e:
            logger.warning("Embedder init failed: {}", e)
            self._embedder = False
        return self._embedder

    def embed(self, text: str) -> Optional[List[float]]:
        """Return a 384-dim embedding of `text`, or None if unavailable."""
        emb = self._ensure_embedder()
        if emb is False or not text:
            return None
        try:
            vec = emb.encode(text[:2000], normalize_embeddings=True)
            return vec.tolist()
        except Exception as e:
            logger.warning("Embedding failed: {}", e)
            return None

    @property
    def embed_dim(self) -> Optional[int]:
        return self._embed_dim

    async def health_check_all(self) -> dict[str, bool]:
        """Check each provider's availability."""
        results = {}
        for name, prov in self.providers.items():
            results[name] = await prov.health_check()
        return results
