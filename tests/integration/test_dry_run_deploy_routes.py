"""
tests/integration/test_dry_run_deploy_routes.py

T2: the deploy endpoints answer with schema-valid canned data when
DRY_RUN_MODE=true, without a GitLab token and without any network call.

These tests build their own app instead of using the session-scoped ``client``
fixture, because DRY_RUN_MODE has to be set before create_app() reads settings
and must not leak into the rest of the suite.

The GitLab SDK is deliberately NOT mocked here: if the dry-run wiring were
wrong, the request would attempt a real connection and fail, which is exactly
the signal these tests exist to give. See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    """A TestClient for an app running in dry-run with no GitLab token."""
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("GITLAB_TOKEN", "")
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()


def _token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, account)}"}


# ── happy paths ───────────────────────────────────────────────────────────────


def test_trigger_returns_200_without_gitlab(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/deploy/stage?action=test-deploy&ref_name=main",
        headers=_auth(dry_run_client),
        json={"variables": []},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["data"]["status"] == "created"
    assert body["data"]["ref_name"] == "main"


def test_check_running_returns_200(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/deploy/stage/check-running?action=test-deploy",
        headers=_auth(dry_run_client),
        json={"variables": []},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # list_running returns [] in dry-run, so a trigger is never blocked by a
    # spurious conflict.
    assert data["has_running"] is False
    assert data["count"] == 0
    assert data["pipelines"] == []


def test_get_pipeline_returns_200(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/deploy/stage/999001", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == 999001


@pytest.mark.parametrize("action", ["cancel", "retry"])
def test_cancel_and_retry_return_200(dry_run_client, action):
    resp = dry_run_client.post(
        f"/api/v1/deploy/stage/999001/{action}", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == 999001


# ── the business logic that must still run ────────────────────────────────────


def test_execution_variable_is_still_injected(dry_run_client):
    """EXECUTION comes from the `action` query param and is load-bearing — a
    regression in that injection must fail the e2e test, not pass silently."""
    resp = dry_run_client.post(
        "/api/v1/deploy/stage?action=my-action",
        headers=_auth(dry_run_client),
        json={"variables": []},
    )
    variables = {v["key"]: v["value"] for v in resp.json()["data"]["variables"]}
    assert variables["EXECUTION"] == "my-action"


def test_service_from_variable_is_still_injected(dry_run_client):
    """SERVICE_FROM carries the authenticated caller's account."""
    resp = dry_run_client.post(
        "/api/v1/deploy/stage?action=test-deploy",
        headers=_auth(dry_run_client, "test_deployer"),
        json={"variables": []},
    )
    variables = {v["key"]: v["value"] for v in resp.json()["data"]["variables"]}
    assert variables["SERVICE_FROM"] == "test_deployer"


def test_body_variables_are_merged(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/deploy/stage?action=test-deploy",
        headers=_auth(dry_run_client),
        json={"variables": [{"key": "FOO", "value": "bar"}]},
    )
    variables = {v["key"]: v["value"] for v in resp.json()["data"]["variables"]}
    assert variables["FOO"] == "bar"
    assert variables["EXECUTION"] == "test-deploy"


def test_trigger_is_not_blocked_by_duplicate_detection(dry_run_client):
    """Duplicate detection still executes, but with no running pipelines it can
    never produce a 409 — otherwise every dry-run trigger after the first would
    fail."""
    for _ in range(3):
        resp = dry_run_client.post(
            "/api/v1/deploy/stage?action=same-action",
            headers=_auth(dry_run_client),
            json={"variables": [{"key": "K", "value": "V"}]},
        )
        assert resp.status_code == 200


# ── deny paths must still hold ────────────────────────────────────────────────


def test_missing_token_still_401(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/deploy/stage?action=test-deploy", json={"variables": []}
    )
    assert resp.status_code == 401


def test_missing_scope_still_403(dry_run_client):
    """test_command holds command_api but not deploy_api."""
    resp = dry_run_client.post(
        "/api/v1/deploy/stage?action=test-deploy",
        headers=_auth(dry_run_client, "test_command"),
        json={"variables": []},
    )
    assert resp.status_code == 403


def test_missing_required_query_param_still_422(dry_run_client):
    """`action` is required; dry-run must not relax Pydantic validation."""
    resp = dry_run_client.post(
        "/api/v1/deploy/stage",
        headers=_auth(dry_run_client),
        json={"variables": []},
    )
    assert resp.status_code == 422
