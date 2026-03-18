"""
app/core/config.py

Multi-environment settings using Pydantic BaseSettings.
The active environment is selected by the APP_ENV environment variable:
  - dev   → loads .env.dev
  - prod  → loads .env.prod
  - test  → loads .env.test
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── App meta ──────────────────────────────────────────────────────────────
    APP_ENV: Literal["dev", "prod", "test"] = "dev"
    APP_NAME: str = "Deploy Service"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── JWT ───────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Storage ───────────────────────────────────────────────────────────────
    USERS_JSON_PATH: str = "data/users.json"

    # ── GitLab ────────────────────────────────────────────────────────────────
    GITLAB_URL: str = "https://gitlab.com/"
    GITLAB_TOKEN: str = ""
    GITLAB_PROJECT_ID: int = 0
    GITLAB_AUTH_JSON_PATH: str = "/data/gitlab_auth.json"

    model_config = SettingsConfigDict(
        # Load order: .env (base) → .env.{APP_ENV} (env-specific overrides).
        # Missing files are silently ignored, so a plain .env alone is enough.
        env_file=[".env", f".env.{os.getenv('APP_ENV', 'dev')}"],
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (loaded once per process)."""
    return Settings()
