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
- **A second isolation layer inside PostgreSQL**: seven tables carry a
  row-level-security policy with `ENABLE` **and** `FORCE`, because
  measurement showed `ENABLE` alone lets the owning role — which is the
  application — see every row. It guards what filtering cannot: a
  cross-tenant *write*. It fails closed, and `scripts/check_setup.py`
  reports whether it is actually in force, since a superuser database
  user bypasses it entirely and silently. Two tables are excluded by
  design and four cannot be protected at all; `app/rls.py` names each and
  says why.
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
- **The free Postgres plan deletes the database after 30 days**, and this
  is the project's most serious constraint. Confirmed from a real
  deployment's dashboard rather than a published source: a database
  created 23 August 2026 carries "Your database will expire on September
  22, 2026. The database will be deleted unless you upgrade to a paid
  instance type." An archive meant to accumulate over months cannot live
  on that plan alone — it needs a paid upgrade, a different provider whose
  free tier does not expire, or a manual restore every month. A weekly `pg_dump` to a GitHub Actions artifact is the
  mitigation, and restoring is a manual `pg_restore`.
- **All scheduling depends on GitHub Actions.** A single point of failure,
  documented as one in [`docs/19-runbook.md`](docs/19-runbook.md) §5.
- **Losing `FIELD_ENCRYPTION_KEY` is unrecoverable** for the fields it
  protects.
- **Row-level security may be enforcing nothing**, silently, if the managed
  provider hands you a superuser database role. The provider decides that,
  not this repository. Run `scripts/check_setup.py` rather than assuming;
  application-level filtering is unaffected either way.
- **Performance was measured at the free tier's ceiling** — 1,200,000
  links, exactly 1.00 GiB — not extrapolated. Realistic searches answer in
  38–202 ms and the dashboard's statistics endpoint in 7.9 ms cached.
  Deep offset pagination degrades to 1.2 s at page 10,000, which is
  recorded and deliberately not fixed. See
  [`docs/37-phase11-measurements.md`](docs/37-phase11-measurements.md),
  including a section on what those numbers cannot tell you.
- **Concurrent database work is capped at 15 requests** — `pool_size +
  max_overflow` in `app/config.py`, not Postgres's own limit of 100.
  Past it a request gets **503 with `Retry-After`** after five seconds
  rather than a long hang and a 500. The capacity is a written choice and
  the failure is explicit.
- **Five endpoints still do database work on the event loop.** Each is
  authenticated, none runs bcrypt, and each awaits real network I/O — the
  reasons they were left alone. They are named in `ASYNC_BY_DESIGN` in
  `tests/test_event_loop_discipline.py`, so a new one has to justify
  itself. `POST /auth/login` used to be among them, and the cost was
  measured: it serialised every login and froze `/healthz` — the path
  `render.yaml` uses as its health check — for 5.7 seconds. See
  [`docs/39-concurrency-measurements.md`](docs/39-concurrency-measurements.md).
- **None of this has run in production yet.** Everything above was
  verified locally against real PostgreSQL. Cold starts, the storage cap,
  GitHub Actions scheduling and a collector account talking to real
  Telegram have not met reality.
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
