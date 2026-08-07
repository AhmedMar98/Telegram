"""
Federated Learning interface — scaffold for v5.0+.

Real federated learning between multiple userbot accounts requires:
    1. A model parameter server (centralized aggregator).
    2. Local training on each userbot's collected data (embeddings, click logs).
    3. Secure aggregation protocol (e.g., SecAgg) to prevent the server
       from inspecting individual gradients.
    4. Differential privacy (DP-SGD) so model updates don't leak training data.

This is non-trivial infrastructure. This module provides:
    - The interface that the classification pipeline calls.
    - A local stub that always returns the global model (no real FL).
    - A clear extension point for future implementations.

Production-grade FL would use Flower (https://flower.dev) or PySyft.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from loguru import logger


@dataclass
class FederatedModelUpdate:
    """A local model update (gradient or weight delta) from one worker."""

    worker_id: str
    round_num: int
    weights_delta: dict[str, list[float]] | None = None
    num_samples: int = 0
    metrics: dict[str, float] = None  # type: ignore


class BaseFederatedAggregator(ABC):
    """Interface for federated learning aggregators."""

    @abstractmethod
    async def submit_update(self, update: FederatedModelUpdate) -> None:
        """A worker submits its local update."""
        ...

    @abstractmethod
    async def get_global_weights(self) -> dict[str, list[float]]:
        """Get the current aggregated model weights."""
        ...

    @abstractmethod
    async def start_round(self, min_workers: int = 2) -> int:
        """Start a new FL round. Returns the round number."""
        ...


class LocalStubAggregator(BaseFederatedAggregator):
    """
    Default aggregator — does NOT actually federate.

    Returns the same global weights every time. Useful for development:
    the code path exists, but no real training happens.

    Replace this with FlowerAggregator or SecAggAggregator in production.
    """

    def __init__(self) -> None:
        self._updates: list[FederatedModelUpdate] = []
        self._round = 0
        self._global_weights: dict[str, list[float]] = {}
        logger.info("[federated] using LocalStubAggregator (no real FL)")

    async def submit_update(self, update: FederatedModelUpdate) -> None:
        self._updates.append(update)
        logger.debug(
            "[federated] received update from {} (round={}, samples={})",
            update.worker_id,
            update.round_num,
            update.num_samples,
        )

    async def get_global_weights(self) -> dict[str, list[float]]:
        return self._global_weights

    async def start_round(self, min_workers: int = 2) -> int:
        self._round += 1
        self._updates.clear()
        logger.info("[federated] starting round {} (min_workers={})", self._round, min_workers)
        return self._round


# Singleton
_aggregator: BaseFederatedAggregator | None = None


def get_federated_aggregator() -> BaseFederatedAggregator:
    """Get the singleton aggregator (default: LocalStubAggregator)."""
    global _aggregator
    if _aggregator is None:
        _aggregator = LocalStubAggregator()
    return _aggregator


def set_federated_aggregator(aggregator: BaseFederatedAggregator) -> None:
    """Override the aggregator (for testing or production deployment)."""
    global _aggregator
    _aggregator = aggregator
