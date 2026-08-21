# Link Intelligence Platform

*[العربية](README.md) — the Arabic README is the complete reference. This is
a condensed English version (idea 237) so the project can be evaluated by
people who do not read Arabic. Where the two disagree, the Arabic one is
authoritative.*

---

A self-hosted platform that **collects links from Telegram channels**,
classifies them, checks whether they still work, and makes them
searchable. It runs at **zero cost** — not "free tier for a while", but
with no component that requires a paid plan or a metered API outside a
permanent free allowance.

## Why it is shaped the way it is

Three constraints drove almost every architectural decision:

| Constraint | Consequence |
|---|---|
| **No Docker, no Oracle Cloud** | Render's native Python runtime, described by `render.yaml` |
| **Zero cost, unconditionally** | No paid plan anywhere, and the LLM classification tier is optional by construction |
| **No free background-worker plan exists** | Everything periodic is a scheduled GitHub Actions job, not a daemon |

That third one is the interesting one. Collection, link-health checking,
pruning, backups and digests are all one-shot scheduled runs. The Telegram
bot uses webhooks rather than polling for the same reason.

The full reasoning, with the alternatives that were considered and what
each choice cost, is in [`docs/24-decisions.md`](docs/24-decisions.md).

## What it actually does

- **Four input paths, one ingestion function.** Telegram channels, a bot
  message, manual paste, or importing browser bookmarks / Pocket /
  Telegram Desktop archives. None of them branches the classification or
  storage logic.
- **Two-tier classification.** Rules always, offline and free. An optional
  free-tier LLM is consulted *only* when rule confidence is low, and its
  absence or failure changes nothing.
- **Link vitality.** Scheduled checks with backoff, distinguishing "never
  checked" from "confirmed dead" — and reporting the latter as *confirmed*
  dead, because a check confirms, it does not witness.
- **Search.** Postgres full-text with relevance ranking in production;
  SQLite `ILIKE` locally. The difference is real, documented, and covered
  by a CI job that runs against **real Postgres**.
- **Notifications with a gate.** Every alert passes one preference check
  before it is sent anywhere — the bot, and optionally an outbound webhook
  you configure yourself.

## Security posture

- Multi-tenant isolation on every row and every query, verified by a sweep
  over **every** endpoint that takes a resource id. A foreign id returns
  **404, never 403** — a 403 would confirm the id exists.
- Two authentication dependencies, not one: endpoints that manage
  credentials or destroy data accept a session cookie only, so a leaked
  API key cannot reach them. This is pinned as a list, so a new endpoint
  fails CI until someone states which side it belongs on.
- `Content-Security-Policy` with `script-src 'self'` — no `unsafe-inline`,
  no `unsafe-eval`. That required removing every inline handler first.
- bcrypt passwords, revocable server-side sessions, optional TOTP with
  recovery codes, Fernet encryption for secrets that must be recoverable.
- Bearer credentials are never logged and never returned by any response.

## Honest limitations

Stated here rather than discovered later:

- **The service sleeps** after ~15 minutes idle on the free plan; the next
  request takes about a minute.
- **The free Postgres plan is time-limited.** The exact duration is *not
  verified in this repository* — published sources disagree and Render's
  site is unreachable from the development environment. Check your Render
  dashboard. A weekly `pg_dump` to a GitHub Actions artifact is the
  mitigation, and restoring is a manual `pg_restore`.
- **All scheduling depends on GitHub Actions.** A single point of failure,
  documented as one in [`docs/19-runbook.md`](docs/19-runbook.md) §5.
- **Losing `FIELD_ENCRYPTION_KEY` is unrecoverable** for the fields it
  protects.
- **Commercial readiness is 2/10**, by the project's own assessment. This
  is a working internal tool, not a product.

## Getting started

[`docs/20-quickstart.md`](docs/20-quickstart.md) — five steps, no Telegram
account, no API keys, no payment method.

## Documentation

| Topic | Document |
|---|---|
| First 30 minutes | [`docs/20-quickstart.md`](docs/20-quickstart.md) |
| Glossary | [`docs/21-glossary.md`](docs/21-glossary.md) |
| FAQ, including the uncomfortable ones | [`docs/22-faq.md`](docs/22-faq.md) |
| Troubleshooting | [`docs/23-troubleshooting.md`](docs/23-troubleshooting.md) |
| Architecture decisions + data-flow diagram | [`docs/24-decisions.md`](docs/24-decisions.md) |
| Operator guide | [`docs/25-operator-guide.md`](docs/25-operator-guide.md) |
| Reading CI results | [`docs/26-reading-ci.md`](docs/26-reading-ci.md) |
| Roadmap, and what will **not** be built | [`docs/27-roadmap.md`](docs/27-roadmap.md) |
| A curl example for every endpoint | [`docs/28-api-examples.md`](docs/28-api-examples.md) |
| Every environment variable | [`docs/29-env-vars.md`](docs/29-env-vars.md) |
| Ideas evaluated and rejected, with reasons | [`docs/06-rejected.md`](docs/06-rejected.md) |
| Incident runbook | [`docs/19-runbook.md`](docs/19-runbook.md) |
| Contributing | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

Most documents are in Arabic. The tables and code in them are readable
without it, and `docs/24-decisions.md` is the one worth translating first
if that becomes necessary.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md). The one non-negotiable rule is not a
style guide:

> **No claim in code, documentation, or a commit message that has not been
> verified against the repository itself.**

## License

MIT.
