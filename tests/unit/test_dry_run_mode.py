"""
tests/unit/test_dry_run_mode.py

Covers the dry-run scaffolding introduced by T1: the DRY_RUN_MODE setting, the
production startup refusal, and the dry_run response marker.

T1 installs the guard rails only — no behaviour is stubbed yet, so these tests
deliberately assert nothing about GitLab or SSH being skipped. See
docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.domain.models import ApiResponse


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """get_settings() is lru_cache'd, so every test that touches DRY_RUN_MODE
    must clear it both before (to drop a cached non-dry-run Settings) and after
    (so later tests don't inherit a dry-run Settings)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── setting ───────────────────────────────────────────────────────────────────


def test_dry_run_mode_defaults_to_false():
    """The flag must be opt-in: a normal process is never in dry-run."""
    assert get_settings().DRY_RUN_MODE is False


def test_dry_run_mode_reads_from_environment(monkeypatch):
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    assert get_settings().DRY_RUN_MODE is True


# ── production refusal ────────────────────────────────────────────────────────


def test_create_app_refuses_dry_run_in_prod(monkeypatch):
    """A prod process must die rather than serve with every write path stubbed.

    A warning would not do: dry-run in production means deploys and commands
    silently do nothing while callers see 200.
    """
    from app.main import create_app

    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="DRY_RUN_MODE"):
        create_app()


def test_create_app_allows_dry_run_outside_prod(monkeypatch):
    """dev/test are the environments dry-run exists for."""
    from app.main import create_app

    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()

    assert create_app() is not None


def test_create_app_allows_prod_without_dry_run(monkeypatch):
    """The guard must only fire on the prod + dry-run combination, not on prod."""
    from app.main import create_app

    monkeypatch.setenv("DRY_RUN_MODE", "false")
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()

    assert create_app() is not None


# ── response marker ───────────────────────────────────────────────────────────


def test_api_response_marker_is_false_by_default():
    """Adding the field must not be a breaking change: existing clients that
    never saw dry_run keep seeing a falsey value."""
    resp = ApiResponse[dict](data={"k": "v"}, request_id="rid")
    assert resp.dry_run is False
    assert resp.model_dump() == {
        "data": {"k": "v"},
        "request_id": "rid",
        "dry_run": False,
    }


def test_api_response_marker_true_in_dry_run(monkeypatch):
    """The whole point of the marker: a 200 in dry-run is self-identifying, so
    a 'successful' response cannot be mistaken for real work."""
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()

    assert ApiResponse[dict](data={}, request_id="rid").dry_run is True


def test_api_response_marker_resolved_per_instance(monkeypatch):
    """The marker is resolved from settings at construction time, which is what
    lets the ~21 ApiResponse(...) call sites stay untouched."""
    assert ApiResponse[dict](data={}).dry_run is False

    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()

    assert ApiResponse[dict](data={}).dry_run is True


def test_api_response_marker_can_be_overridden_explicitly(monkeypatch):
    """An explicit value must win over the settings-derived default, so a
    caller (or a test) can construct either shape deliberately."""
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()

    assert ApiResponse[dict](data={}, dry_run=False).dry_run is False
