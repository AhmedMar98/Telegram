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
    max_accounts_per_workspace: int = 5

    # --- Telegram bot (webhook mode; no polling worker required) --------
    bot_token: str | None = None
    bot_webhook_secret: str | None = None
    public_base_url: str | None = None  # e.g. https://link-intel-web.onrender.com

    # --- Optional free-tier LLM classification tier ----------------------
    # Left empty by default: the platform is fully functional (rules-based
    # classification) with zero external API calls and zero cost. Setting
    # this key only *adds* accuracy on top of the free tier, using Groq's
    # free API tier, and never blocks a request if it is absent or fails.
    groq_api_key: str | None = None
    groq_model: str = "llama-3.1-8b-instant"

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

    # Idea 160. Warn while there is still enough quota left to matter: at
    # 10% of a daily allowance there is usually most of a day to react, and
    # a threshold much lower would fire only once the tier had effectively
    # already stopped. Only ever consulted when a Groq response actually
    # reported a limit — see app/classifier/llm.py.
    groq_quota_alert_fraction: float = 0.1

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
