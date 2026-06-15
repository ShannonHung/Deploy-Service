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
    # TCP-level timeout passed to the python-gitlab HTTP client. Covers all
    # API calls (trigger, get, list, cancel, retry). Set high enough to
    # tolerate a slow internal GitLab instance while still being shorter than
    # any upstream ingress timeout so we return our own 504 first.
    GITLAB_HTTP_TIMEOUT_SECONDS: int = 120
    # Upper bound on a single GitLab trace fetch (asyncio-level guard).
    # Should be ≤ GITLAB_HTTP_TIMEOUT_SECONDS.
    GITLAB_TRACE_TIMEOUT_SECONDS: int = 45
    # Cache TTL for finished-job traces (immutable). A poll for a cached
    # finished job hits Redis and skips GitLab entirely.
    GITLAB_TRACE_CACHE_TTL_SECONDS: int = 86400
    # Soft cap: trace size at which the viewer shows a "log is large"
    # banner pointing to GitLab but keeps rendering. UX hint only.
    GITLAB_TRACE_SOFT_CAP_BYTES: int = 5 * 1024 * 1024
    # Hard cap: trace size at which the service stops returning lines and
    # the viewer switches to the fatal-error panel with a GitLab link.
    # Protects pod memory from runaway logs.
    GITLAB_TRACE_HARD_CAP_BYTES: int = 10 * 1024 * 1024

    # ── SSH Command API ───────────────────────────────────────────────────────
    COMMAND_CONFIG_DIR: str = "data"
    COMMAND_DEFAULT_TIMEOUT: int = 30
    COMMAND_KILL_GRACE_SECONDS: int = 2
    COMMAND_MAX_CONCURRENCY: int = 20
    COMMAND_MAX_RUNNING: int = 50
    SSH_CONNECT_TIMEOUT_SECONDS: int = 30

    # ── Inventory API ─────────────────────────────────────────────────────────
    INVENTORY_API_URL: str = "http://localhost:9001"
    INVENTORY_API_TOKEN: str = "fake-inventory-token"
    INVENTORY_API_TIMEOUT_SECONDS: float = 5.0

    # ── Cluster / Bastion mapping API ─────────────────────────────────────────
    CLUSTER_API_URL: str = "http://localhost:9001"
    CLUSTER_API_TOKEN: str = "fake-cluster-token"
    CLUSTER_API_TIMEOUT_SECONDS: float = 5.0
    BASTION_DEFAULT_TYPE: str = "type1"

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    COMMAND_RESULT_TTL_SECONDS: int = 86400

    model_config = SettingsConfigDict(
        # Load order: .env (base) → .env.{APP_ENV} (env-specific overrides).
        # Missing files are silently ignored, so a plain .env alone is enough.
        env_file=[".env", f".env.{os.getenv('APP_ENV', 'dev')}", ".env.local"],
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (loaded once per process)."""
    return Settings()
