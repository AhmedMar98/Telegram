# Link Intelligence Platform

*[العربية](DOCS_CONSOLIDATED.md) — the Arabic document is the complete,
single reference for this project. This is a condensed English overview
(not a full translation) so the project can be evaluated by people who do
not read Arabic. Where the two disagree, the Arabic document is
authoritative.*

---

A self-hosted platform that **collects links from Telegram channels**,
classifies them, checks whether they still work, and makes them
searchable. It runs at **zero cost** — not "free tier for a while", but
with no component that requires a paid plan or a metered API outside a
permanent free allowance.

## The shape of it

Three constraints drove almost every architectural decision:

| Constraint | Consequence |
|---|---|
| **No Docker, no Oracle Cloud** | Render's native Python runtime, described by `render.yaml` |
| **Zero cost, unconditionally** | No paid plan anywhere, and no external API in any code path: classification is rules-only, in-process |
| **No free background-worker plan exists** | Everything periodic is a scheduled GitHub Actions job. One exception: an optional near-instant listener that runs as a task *inside* the web process itself |

Collection, link-health checking, pruning, backups and digests are all
one-shot scheduled runs. The Telegram bot uses webhooks rather than
polling for the same reason.

## What it does

- **Four input paths, one ingestion function.** Telegram channels, a bot
  message, manual paste, or importing browser bookmarks / Pocket /
  Telegram Desktop archives — none of them branches the classification or
  storage logic.
- **Evidence-based classification.** Every signal a link carries —
  extension, domain, path segment, source channel's title, words in the
  message, sibling links in the same message — is weighed together rather
  than the first match winning. Offline, free, deterministic.
- **Link vitality.** Scheduled checks with backoff, distinguishing "never
  checked" from "confirmed dead."
- **Search.** Postgres full-text with relevance ranking in production;
  SQLite `ILIKE` locally, covered by a CI job against real Postgres.
- **Notifications with a gate.** Every alert passes one preference check
  before it is sent anywhere.

## Honest limitations

- The service sleeps after ~15 minutes idle on the free plan.
- The free Postgres plan deletes the database after 30 days — the
  project's most serious constraint. A weekly encrypted backup is the
  mitigation; restoring is a manual `pg_restore`.
- All scheduling depends on GitHub Actions — a single point of failure.
- Losing `FIELD_ENCRYPTION_KEY` is unrecoverable for the fields it
  protects.
- Commercial readiness is **2/10**, by the project's own assessment. This
  is a working internal tool, not a product.

## Documentation

**Everything else — deployment, security, API, environment variables,
troubleshooting, architecture decisions, governance, roadmap — lives in
one place:** [`DOCS_CONSOLIDATED.md`](DOCS_CONSOLIDATED.md). It replaced
44 separate files by the repository owner's explicit decision, so that
editing the documentation never means navigating between files.

## Contributing

See `DOCS_CONSOLIDATED.md` §35. The one non-negotiable rule is not a
style guide:

> **No claim in code, documentation, or a commit message that has not been
> verified against the repository itself.**

## License

MIT.
