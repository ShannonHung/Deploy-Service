"""
tests/conftest.py

Shared pytest fixtures.
Sets APP_ENV=test before any import so Settings picks up .env.test.
"""

from __future__ import annotations

import os
import pytest

# Must be set BEFORE importing app modules that call get_settings()
os.environ.setdefault("APP_ENV", "test")

from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture(scope="session")
def client() -> TestClient:
    """TestClient backed by the full app with test settings."""
    app = create_app()
    return TestClient(app)
