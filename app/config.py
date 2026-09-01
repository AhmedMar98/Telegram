"""Application configuration loaded from environment variables.

All secrets (Telegram bot token, database URL, signing key, ...) are read
from the environment so the exact same code runs locally, in tests, in
GitHub Actions, and on Render without any file-based secret ever being
committed to the repository.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Rewrite a hosted-Postgres URL to the driver actually installed.

    Managed providers (Render included) hand out connection strings of the
    form ``postgres://…``. Two things go wrong with that verbatim:

    1. SQLAlchemy 2 removed the ``postgres://`` alias, so it fails with
       "Can't load plugin: sqlalchemy.dialects:postgres".
    2. Even corrected to ``postgresql://``, SQLAlchemy resolves the default
       driver to psycopg2, which is not installed — this project ships
       psycopg 3 — so it fails with "No module named 'psycopg2'".

    Both are startup-time crashes on a fresh deploy, so the URL is
    normalized here, once, for every consumer: the web app, Alembic, the
    collector, and the setup diagnostic.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core -----------------------------------------------------------
    app_name: str = "Link Intelligence Platform"
    environment: str = "development"  # development | production
    database_url: str = "sqlite:///./local.db"
    secret_key: str = "dev-secret-key-change-me"

    # --- Connection pool --------------------------------------------------
    #
    # These were SQLAlchemy's defaults until a load measurement made them
    # visible (docs/39). They are written out here because the numbers they
    # replace were never chosen by anyone, and one of them was actively
    # harmful.
    #
    # ``pool_size`` + ``max_overflow`` is the real ceiling on concurrent
    # requests that touch the database — **15**, not the 100 that Postgres
    # itself allows (docs/07 §5). The application runs out of connections
    # long before the database does, which is why idea 280's answer is a
    # property of this file rather than of the hosting plan.
    #
    # 15 is kept, so ``db_pool_size`` and ``db_max_overflow`` restate
    # SQLAlchemy's own defaults and change no behaviour. That is deliberate
    # and worth saying plainly: writing the ceiling down where an operator
    # will look for it *is* the deliverable, because the number decides
    # what happens under load and nobody had chosen it. Only
    # ``db_pool_timeout_seconds`` below actually changes anything.
    #
    # Why 15 rather than more: the free web instance is a single uvicorn
    # worker on a fraction of a CPU; letting it open fifty concurrent
    # queries would not make it finish them faster, and every Postgres
    # connection costs memory on a 1 GB database plan.
    #
    # One interaction to know before turning ``db_pool_size`` down: it is
    # no longer the *first* ceiling. FastAPI runs plain ``def`` endpoints
    # in anyio's threadpool, which is capped at 40 threads, so at most 40
    # requests can be queueing for these 15 connections. Measured at 40
    # concurrent logins, zero requests hit the pool timeout (docs/39 §4),
    # so the two are not currently in tension — but lowering one without
    # the other is how that changes.
    #
    # ``pool_timeout`` is the one that changed, from **30 seconds to 5**.
    # Measured at 20 concurrent logins: the five requests past the ceiling
    # each waited the full thirty seconds and then failed with a 500. A
    # request held for thirty seconds is worse than a request refused in
    # five — it occupies a worker, it outlives the caller's patience, and
    # on this deployment it outlives the platform's health check, which is
    # how a capacity problem turns into a restart. Five seconds is long
    # enough to ride out a brief burst and short enough to stay a burst.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 5

    # --- Auth -------------------------------------------------------------
    # An invite code is required to self-register because the deployed
    # instance is reachable on a public Render URL even though the product
    # is an internal/private tool. Leave unset to disable open registration
    # entirely (accounts must then be created manually).
    invite_code: str | None = None
    session_ttl_hours: int = 24 * 14  # 14 days
    # bcrypt work factor. 12 is a sane default on real hardware; it is
    # exposed so the (slow, shared) free-tier CPU can be tuned down if login
    # latency becomes painful, and so the test suite can drop it to the
    # minimum instead of burning CI minutes on deliberate key stretching.
    bcrypt_rounds: int = 12

    # --- Collecting accounts ---------------------------------------------
    # Cap on Telegram accounts per workspace. Not a licensing limit: each
    # account is a real Telegram login whose session string sits encrypted
    # in the database, so an unbounded number is an unbounded pile of
    # bearer credentials. Configurable because the right number depends on
    # how many channels a deployment follows, not on anything this code
    # can know.
    #
    # Raised 5 -> 10 by owner decision. The number is a deliberate ceiling,
    # not a guess: each account is one more real Telegram login whose
    # session string sits encrypted in the database, and at 20 channels per
    # account (DEFAULT_MAX_CHANNELS_PER_ACCOUNT in scripts/collect.py) ten
    # accounts already cover 200 channels — well past what one person
    # follows. Raising it further multiplies the bearer-credential blast
    # radius for capacity nobody has asked for.
    max_accounts_per_workspace: int = 10

    # --- Live collection --------------------------------------------------
    # Off by default, and the default is the conservative one on purpose.
    #
    # When on, app/live.py holds an open Telegram connection inside the web
    # process and stores links the second they are posted, instead of on
    # the hourly cron tick. It does not replace the cron job: the listener
    # is best-effort (the free instance sleeps, connections drop) and the
    # scheduled collector remains the guarantee that nothing was missed.
    #
    # Default False because turning it on has three preconditions this
    # code cannot check for you — TG_API_ID/TG_API_HASH/COLLECTOR_WORKSPACE_ID
    # present in the *web* service's environment, telethon installed there,
    # and something keeping the instance awake. Defaulting to True would
    # mean every existing deployment starts logging a listener failure it
    # never asked for.
    live_collector_enabled: bool = False

    # --- What the collector is allowed to read ---------------------------
    # A row in `channels` used to arrive one way only: typed into the
    # dashboard. That made "collect from Telegram" mean "collect from the
    # handful you remembered to add", while the groups and private chats
    # that carry most links stayed unreachable.
    #
    # With discovery on, every run asks the account which dialogs it has
    # and registers the ones it does not know yet. Left on by default
    # because a collector that silently reads a fraction of what the
    # account can see is the more surprising of the two behaviours.
    collector_auto_discover: bool = True

    # Which dialog kinds discovery may register: any comma-separated
    # subset of channel, group, private, bot — or "all".
    #
    # **Defaults to channels and groups only.** It used to default to
    # "all", which meant a deployment collected personal one-to-one
    # conversations because nobody had typed a value, not because anybody
    # had decided to. A stored link carries up to 2,000 characters of the
    # message around it, including what the other person wrote, so
    # `private` is a real privacy decision and now has to be made
    # explicitly. `bot` is opt-in for a different reason: bot feeds are
    # high-volume and would swamp a 1 GiB database with content nobody
    # chose to follow.
    collector_scope: str = "channel,group"

    # Cap on dialogs one discovery pass may register per account. Bounds
    # the first run on an account that has hundreds of them; the rest are
    # picked up on the runs that follow, because discovery is incremental
    # and never re-registers what it already knows.
    collector_max_dialogs: int = 200

    # How many sources the assignment engine gives one account before it
    # starts reporting overflow (app/assignment.py).
    #
    # **An operating limit, not a safety claim.** What an account can carry
    # depends on the dialogs, their message rate, the account's age and
    # Telegram's limits on the day — none of which this system measures. So
    # it is configurable, and no number here is promised to be safe.
    assignment_capacity_per_account: int = 200

    # --- proactive pacing (anti-ban) ------------------------------------
    #
    # Until now the only defence against a ban was reactive: catch
    # FloodWaitError after Telegram has already decided you were reading
    # too fast. This adds the missing half — a randomised pause between
    # dialogs so the request pattern does not look mechanical in the first
    # place.
    #
    # Randomised rather than fixed on purpose: a constant 3-second gap is
    # itself a signature, and the thing being avoided is *looking like a
    # program*.
    collector_pace_min_seconds: float = 1.5
    collector_pace_max_seconds: float = 4.0

    # The pacing budget, and the reason this is not simply "sleep between
    # every dialog".
    #
    # Naively: 4s x 20 dialogs x 10 accounts = up to 800 seconds added to a
    # job that runs hourly, on GitHub Actions runners that are already
    # late under load. The shield would cause the outage it exists to
    # prevent, and a timed-out run looks exactly like a broken collector.
    #
    # So pacing is spent from a budget. While there is budget the collector
    # paces; when it runs out the collector stops pacing and finishes the
    # dialogs it has left — the run completes either way, and the rotation
    # picks up the rest next hour.
    collector_pace_budget_seconds: float = 240.0

    # --- Telegram bot (webhook mode; no polling worker required) --------
    # Which chats may talk to the bot at all. Comma-separated chat ids.
    #
    # Empty means "any chat may talk to it", which is what it has always
    # done and what every existing deployment depends on — so an empty
    # value cannot become a lockout on upgrade. Setting it turns the bot
    # private in the strong sense: an unlisted chat gets no reply, not
    # even the help text, so the bot's existence is not confirmed to
    # someone who found it by guessing.
    #
    # Note what this is *not*: the link code already prevents an unlisted
    # chat from reading anyone's data. This closes the smaller gap of a
    # stranger being able to interact with the bot at all.
    bot_allowed_chat_ids: str = ""

    # Whether the bot may onboard a collection account (phone + OTP).
    #
    # Off by default, and the reason is what actually travels through a
    # chat when it is on. NOT the api_hash or the session string — those
    # never leave the server. But the one-time code does, and so does the
    # two-factor password if the account has one, and Telegram keeps both
    # in the chat history until something deletes them. The flow deletes
    # each message the moment it is read and refuses to run anywhere but a
    # one-to-one chat, but an operator should still choose to enable it.
    bot_account_onboarding: bool = False

    # --- lead detection (the second product) -----------------------------
    #
    # Off by default, and this one is not a convenience flag either.
    # Everything stored until now was a link. With this on, the system also
    # stores identifiable third parties who never opted in — their Telegram
    # id, handle, display name and the text of what they asked for. That is
    # a decision a deployment makes, not a default it discovers.
    leads_enabled: bool = False

    # How long a matched message is kept, in days. 0 disables the purge.
    #
    # This table holds other people's words. Keeping them forever by
    # default is what turns a lead pipeline into an archive nobody agreed
    # to, so there is a window and it is short. The beneficiary row and its
    # counter survive the purge, so "this person has asked four times"
    # outlives the texts.
    leads_retention_days: int = 90

    bot_token: str | None = None
    bot_webhook_secret: str | None = None
    public_base_url: str | None = None  # e.g. https://link-intel-web.onrender.com

    # --- Deploy identity (idea 187) ----------------------------------------
    # Render sets RENDER_GIT_COMMIT on every build from a repository, so
    # the running code identifies itself without anything being written by
    # hand at release time — a version string somebody has to remember to
    # bump is a version string that is eventually wrong.
    render_git_commit: str | None = None
    render_service_name: str | None = None

    # --- Storage headroom (idea 153) ---------------------------------------
    # The size at which the storage alert fires, as a fraction of this
    # limit. **The limit is a configured assumption, not a verified fact:**
    # Render's site is unreachable from the development environment (see
    # docs/02 §5, which records the same problem for the database's
    # lifetime), so this default is a commonly cited figure rather than
    # one this project measured. It is settable precisely because it is
    # unverified — and the alert says so rather than presenting it as
    # Render's number.
    storage_limit_bytes: int = 1_073_741_824  # 1 GiB
    storage_alert_fraction: float = 0.8

    # --- Field-level encryption -------------------------------------------
    # Encrypts secrets that (unlike passwords or session tokens) must be
    # recoverable in their original form — today, only a collected Telegram
    # account's session string (app/crypto.py). The dev default below is
    # published in this repository and MUST be overridden with a real
    # secret (e.g. `python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"`) anywhere real credentials are
    # stored; using the default in production makes the encryption
    # decorative.
    field_encryption_key: str = "S7uvgQ59s2Xo-V2u3yZdnqZLxhnienyS6rirAOJ_pnA="

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("field_encryption_key")
    @classmethod
    def _default_field_encryption_key_when_unset(cls, value: str) -> str:
        # An unset GitHub Actions secret still exports an empty-string env
        # var (${{ secrets.X }} with no X resolves to ""), which would
        # otherwise reach Fernet() as an invalid key and crash the
        # collector outright instead of falling back like every other
        # optional secret in this file does.
        return value or "S7uvgQ59s2Xo-V2u3yZdnqZLxhnienyS6rirAOJ_pnA="

    @property
    def is_saas_ready_auth(self) -> bool:
        """Whether workspace-isolated multi-tenant auth is active (always true)."""
        return True


@lru_cache
def get_settings() -> Settings:
    return Settings()


# The two secrets in this file that ship with a real, working default value
# rather than an empty one — because SECRET_KEY signs every session cookie
# and FIELD_ENCRYPTION_KEY must decrypt what earlier writes encrypted with
# it, neither can crash on missing input the way an optional integration
# key does. Kept as a single dict (not read back out of the two field
# defaults above) so this file has exactly one place that says what "still
# the default" means; a default-value change above without a matching
# change here would silently stop being caught, so keep them in sync.
_PUBLISHED_DEFAULTS: dict[str, str] = {
    "SECRET_KEY": "dev-secret-key-change-me",
    "FIELD_ENCRYPTION_KEY": "S7uvgQ59s2Xo-V2u3yZdnqZLxhnienyS6rirAOJ_pnA=",
}


def production_secrets_check(settings: Settings) -> list[str]:
    """Names of secrets still at their published default, in production.

    Both defaults above are committed to this public repository, so using
    either of them in production is not weak security, it is *no* security:
    a signed session cookie anyone can forge, and field encryption anyone
    can reverse. That was previously only a comment ("MUST be overridden")
    next to the field definition, which nothing enforced.

    Returns a list, not a bool, because "SECRET_KEY and FIELD_ENCRYPTION_KEY
    are both still the published default" is an actionable message and
    "some secrets are weak" is not; the caller (lifespan, and the setup
    diagnostic) prints exactly what to fix.

    Development is always given a clean pass: the published defaults are
    what makes ``uvicorn app.main:app`` work with no .env file on a fresh
    checkout, and the test suite depends on that. Only ``environment ==
    "production"`` turns this from documentation into an enforced contract.
    """
    if settings.environment != "production":
        return []
    return published_defaults_in_use(settings)


def published_defaults_in_use(
    settings: Settings,
    *,
    names: tuple[str, ...] = ("SECRET_KEY", "FIELD_ENCRYPTION_KEY"),
) -> list[str]:
    """Which of the named secrets still carry the value published here.

    Deliberately **not** gated on ``environment``: the caller decides what
    a published default means for its own job. The web app only cares in
    production, because development legitimately runs on these values. A
    scheduled job that encrypts a real Telegram session string cares
    always — nothing about ``ENVIRONMENT`` changes the fact that a key
    printed in this repository cannot protect a bearer credential, and no
    workflow in .github/workflows sets ``ENVIRONMENT`` at all, so gating
    on it there would have made the check permanently silent.
    """
    values = {
        "SECRET_KEY": settings.secret_key,
        "FIELD_ENCRYPTION_KEY": settings.field_encryption_key,
    }
    return [name for name in names if values[name] == _PUBLISHED_DEFAULTS[name]]


def require_real_secrets(settings: Settings, *, names: tuple[str, ...], job: str) -> None:
    """Refuse to run a job that would handle real credentials with a published key.

    Raised rather than logged, and raised *before* any work starts, because
    the damage is done the moment a session string is written encrypted
    under a key anyone can read off this repository — at that point the row
    is plaintext to anybody who obtains the database, and re-encrypting it
    later does not un-leak whatever was already exposed.

    ``job`` names the caller in the message so the operator reading a
    GitHub Actions log knows which workflow to fix without opening code.
    """
    problems = published_defaults_in_use(settings, names=names)
    if not problems:
        return
    raise RuntimeError(
        f"{job}: refusing to run with published default(s): {', '.join(problems)}. "
        "These values are committed to this repository, so anything encrypted or signed "
        "with them is readable by anyone who has the code. Set the real value(s) as "
        "repository secrets before the next scheduled run. Generate FIELD_ENCRYPTION_KEY "
        'with: python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    )
