"""
Prometheus metrics endpoint.

Exposes /metrics in Prometheus text exposition format. Counters track
collect/classify/vitality/LLM activity; gauges expose current DB state;
histograms measure latency distributions.

Uses prometheus_client (pure-Python, no extra infra required).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Response
from loguru import logger

from ..database import SessionLocal
from ..models import Link, LinkStatus, LLMProviderState, SemanticCache


router = APIRouter()


# Lazy init — only import prometheus_client if /metrics is actually hit
_metrics = None


def _init_metrics():
    global _metrics
    if _metrics is not None:
        return _metrics
    try:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            generate_latest,
        )
    except ImportError:
        logger.warning("prometheus_client not installed; /metrics disabled")
        return None

    registry = CollectorRegistry()
    _metrics = {
        "registry": registry,
        "links_collected_total": Counter(
            "link_intel_links_collected_total",
            "Total links collected from Telegram",
            registry=registry,
        ),
        "links_classified_total": Counter(
            "link_intel_links_classified_total",
            "Total links classified",
            ["tier"],
            registry=registry,
        ),
        "links_vitality_checked_total": Counter(
            "link_intel_links_vitality_checked_total",
            "Total vitality checks performed",
            ["result"],  # alive|dead
            registry=registry,
        ),
        "llm_calls_total": Counter(
            "link_intel_llm_calls_total",
            "Total LLM API calls",
            ["provider", "status"],  # status: success|rate_limited|error
            registry=registry,
        ),
        "semantic_memory_hits_total": Counter(
            "link_intel_semantic_memory_hits_total",
            "Total semantic memory cache hits",
            registry=registry,
        ),
        "semantic_memory_misses_total": Counter(
            "link_intel_semantic_memory_misses_total",
            "Total semantic memory cache misses",
            registry=registry,
        ),
        "active_links": Gauge(
            "link_intel_active_links",
            "Current non-archived links count",
            registry=registry,
        ),
        "alive_links": Gauge(
            "link_intel_alive_links",
            "Current alive links count",
            registry=registry,
        ),
        "dead_links": Gauge(
            "link_intel_dead_links",
            "Current dead links count",
            registry=registry,
        ),
        "llm_provider_healthy": Gauge(
            "link_intel_llm_provider_healthy",
            "LLM provider health (1=healthy, 0=unhealthy)",
            ["provider"],
            registry=registry,
        ),
        "semantic_cache_entries": Gauge(
            "link_intel_semantic_cache_entries",
            "Total entries in semantic cache",
            registry=registry,
        ),
        "classify_latency_seconds": Histogram(
            "link_intel_classify_latency_seconds",
            "Classification pipeline latency",
            ["tier"],
            buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0),
            registry=registry,
        ),
        "llm_latency_seconds": Histogram(
            "link_intel_llm_latency_seconds",
            "LLM call latency per provider",
            ["provider"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
            registry=registry,
        ),
    }
    return _metrics


def record_link_collected() -> None:
    m = _init_metrics()
    if m:
        m["links_collected_total"].inc()


def record_classified(tier: str, latency_seconds: float) -> None:
    m = _init_metrics()
    if m:
        m["links_classified_total"].labels(tier=tier).inc()
        m["classify_latency_seconds"].labels(tier=tier).observe(latency_seconds)


def record_vitality(result: str) -> None:
    m = _init_metrics()
    if m:
        m["links_vitality_checked_total"].labels(result=result).inc()


def record_llm_call(provider: str, status: str, latency_seconds: Optional[float] = None) -> None:
    m = _init_metrics()
    if m:
        m["llm_calls_total"].labels(provider=provider, status=status).inc()
        if latency_seconds is not None:
            m["llm_latency_seconds"].labels(provider=provider).observe(latency_seconds)


def record_semantic_hit(hit: bool) -> None:
    m = _init_metrics()
    if m:
        if hit:
            m["semantic_memory_hits_total"].inc()
        else:
            m["semantic_memory_misses_total"].inc()


def _refresh_gauges() -> None:
    """Refresh DB-backed gauges from current DB state."""
    m = _init_metrics()
    if not m:
        return
    try:
        with SessionLocal() as db:
            m["active_links"].set(
                db.query(Link).filter(Link.archived.is_(False)).count()
            )
            m["alive_links"].set(
                db.query(Link).filter(Link.alive.is_(True)).count()
            )
            m["dead_links"].set(
                db.query(Link).filter(Link.alive.is_(False)).count()
            )
            m["semantic_cache_entries"].set(db.query(SemanticCache).count())

            for p in db.query(LLMProviderState).all():
                m["llm_provider_healthy"].labels(provider=p.name).set(
                    1 if p.is_healthy and not (p.cooldown_until and p.cooldown_until > __import__("datetime").datetime.utcnow()) else 0
                )
    except Exception as e:
        logger.debug("[metrics] gauge refresh failed: {}", e)


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus exposition endpoint."""
    m = _init_metrics()
    if m is None:
        return Response(
            "# prometheus_client not installed\n",
            media_type="text/plain",
            status_code=503,
        )
    _refresh_gauges()
    from prometheus_client import generate_latest
    return Response(
        generate_latest(m["registry"]),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
