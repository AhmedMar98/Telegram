# Federated Learning — Design Document

> Status: **Scaffolded** (interface only; real FL deferred to v5.1+)

## Why defer?

Real federated learning between multiple userbot accounts requires:

1. **Parameter server** (centralized aggregator) — extra infrastructure.
2. **Local training** on each userbot's collected data (embeddings, click logs).
3. **Secure aggregation** (SecAgg) so the server can't inspect individual gradients.
4. **Differential privacy** (DP-SGD) so updates don't leak training data.
5. **Network protocol** for worker→server weight sync.

This is non-trivial. For v5.0 we ship the interface (`app/federated/__init__.py`)
with a `LocalStubAggregator` that always returns the global model unchanged.
The code path exists; real FL can be added later without touching the pipeline.

## Use case

The classification pipeline could improve over time by learning from:
- Click feedback (alive links clicked more = "good" classification)
- Manual corrections via the web panel
- New link patterns emerging in channels

Today, this is handled by the **Semantic Memory cache** (similarity-based reuse),
which is a simpler form of "learning from past classifications."

## Implementation path (future)

1. **Phase 1**: Replace `LocalStubAggregator` with `FlowerAggregator` using
   the [Flower](https://flower.dev) framework. Each userbot runs a Flower client.
2. **Phase 2**: Add DP-SGD via Opacus to limit per-update information leakage.
3. **Phase 3**: Use SecAgg (flower-secagg) for cryptographic aggregation.
4. **Phase 4**: Train a small custom classifier head on top of MiniLM embeddings,
   federated across all userbots.

## Threat model

- **Honest-but-curious server**: SecAgg prevents the aggregator from seeing
  individual updates (only the sum, which is mathematically useless).
- **Malicious worker**: DP-SGD bounds the influence of any single update.
- **Data poisoning**: Requires Byzantine-robust aggregation (e.g., Krum).
  Out of scope for v5.0; revisit if poisoning attacks are observed.
