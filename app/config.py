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

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def is_saas_ready_auth(self) -> bool:
        """Whether workspace-isolated multi-tenant auth is active (always true)."""
        return True


@lru_cache
def get_settings() -> Settings:
    return Settings()
