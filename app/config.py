"""
Centralized configuration using pydantic-settings.

All secrets come from environment variables or a local .env file.
NEVER hardcode secrets in source code.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root (one level above /app)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"


class Settings(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))

    # --- Database (SQLite — no Docker) ---
    # Default to absolute path under data/ to avoid cwd issues
    _db_path = str(DATA_DIR / "link_intel.db")
    database_url: str = f"sqlite:///{_db_path}"
    sqlite_path: str = _db_path

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Telegram userbot ---
    tg_api_id: str = ""
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_session_name: str = "userbot_main"
    tg_monitor_channels: str = ""  # comma-separated
    tg_mock_mode: bool = True

    # --- Telegram bot ---
    tg_bot_token: str = ""

    # --- LLM providers ---
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemma-4-26b-a4b-it:free"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"
    hf_api_key: str = ""
    hf_model: str = "mistralai/Mistral-7B-Instruct-v0.3"
    zai_api_key: str = ""
    zai_model: str = "glm-4-flash"
    # v5.0 additional providers
    cohere_api_key: str = ""
    cohere_model: str = "command-r-08-2024"
    mistral_api_key: str = ""
    mistral_model: str = "mistral-small-latest"
    together_api_key: str = ""
    together_model: str = "meta-llama/Llama-3-8b-chat-hf"
    anyscale_api_key: str = ""
    anyscale_model: str = "meta-llama/Llama-3.1-8B-Instruct"
    cloudflare_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_model: str = "@cf/meta/llama-3.1-8b-instruct"

    # v5.1 NVIDIA Integrate API: https://build.nvidia.com → "Get API Key"
    # Supports: mistralai/mistral-nemotron (tested working),
    #           meta/llama-3.3-70b-instruct, deepseek-ai/deepseek-v4-pro,
    #           z-ai/glm-5.2 (may cold-start slowly), thinkingmachines/inkling
    nvidia_api_key: str = ""
    nvidia_model: str = "mistralai/mistral-nemotron"

    # --- Stripe billing (v5.0) ---
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_free: str = ""
    stripe_price_pro: str = ""
    stripe_price_enterprise: str = ""

    # --- Classification ---
    semantic_reuse_threshold: float = 0.92
    vitality_rate_per_min: int = 6

    # --- Collector behavior ---
    collect_min_delay_sec: int = 8
    collect_max_delay_sec: int = 25
    collect_active_hours_start: int = 8
    collect_active_hours_end: int = 23

    # --- Encryption ---
    aes_encryption_key: str = ""  # 64-char hex = 32 bytes
    hmac_key: str = ""

    # --- Web auth ---
    web_admin_username: str = "admin"
    web_admin_password: str = "change-me"

    # --- Oracle deploy ---
    oracle_host: str = ""
    oracle_user: str = "ubuntu"
    oracle_ssh_key: str = ""
    oracle_deploy_path: str = "/home/ubuntu/link-intelligence"

    # --- Derived helpers ---
    @property
    def monitor_channels_list(self) -> List[str]:
        if not self.tg_monitor_channels.strip():
            return []
        return [c.strip() for c in self.tg_monitor_channels.split(",") if c.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @field_validator("aes_encryption_key")
    @classmethod
    def _validate_aes_key(cls, v: str) -> str:
        if v and len(v) != 64:
            raise ValueError("AES_ENCRYPTION_KEY must be 64 hex chars (32 bytes)")
        return v

    @field_validator("hmac_key")
    @classmethod
    def _validate_hmac_key(cls, v: str) -> str:
        if v and len(v) != 64:
            raise ValueError("HMAC_KEY must be 64 hex chars (32 bytes)")
        return v

    def ensure_directories(self) -> None:
        """Create runtime directories if missing."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor."""
    s = Settings()
    s.ensure_directories()
    return s
