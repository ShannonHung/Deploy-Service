"""
tests/integration/test_dry_run_command_routes.py

T4: a synchronous SSH command runs end to end when DRY_RUN_MODE=true, opening
no SSH connection and requiring no key material on disk.

The load-bearing assertions here are the ones proving dry-run did NOT weaken
the security boundary. CommandExecutor validates (whitelist → argument regex →
anti-injection → pipeline construction) strictly *before* _connect, and dry-run
replaces only _connect's return value — so every one of those checks must still
reject. A dry-run that answered 200 to a non-whitelisted command would be worse
than no test at all, because an e2e suite would display it as "security checks
passing".

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

# The async execution path persists state in Redis.
pytestmark = pytest.mark.e2e


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _execute(client: TestClient, headers: dict[str, str], **overrides):
    body = {
        "host": "node1",
        "host_type": "hostname",
        "username": "root",
        "port": 22,
        "command_name": "net_info",
    }
    body.update(overrides)
    return client.post("/api/v1/command/execution", headers=headers, json=body)


def _poll_until_done(client, headers, command_id, attempts: int = 40):
    for _ in range(attempts):
        time.sleep(0.2)
        data = client.get(
            f"/api/v1/command/execution/{command_id}", headers=headers
        ).json()["data"]
        if data["status"] != "running":
            return data
    pytest.fail(f"command {command_id} never left running")


# ── happy path ────────────────────────────────────────────────────────────────


def test_execute_returns_200_without_ssh(dry_run_client):
    resp = _execute(dry_run_client, _auth(dry_run_client))
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True


def test_execute_reaches_a_successful_terminal_state(dry_run_client):
    headers = _auth(dry_run_client)
    resp = _execute(dry_run_client, headers)
    command_id = resp.json()["data"]["command_id"]

    final = _poll_until_done(dry_run_client, headers, command_id)
    assert final["status"] == "success"
    assert final["exit_status"] == 0


def test_execute_needs_no_ssh_key_material(dry_run_client):
    """A dry-run instance must hold no production secrets, so SSH config
    loading is satisfied synthetically rather than from disk."""
    resp = _execute(dry_run_client, _auth(dry_run_client), ssh_config="no-such-target")
    assert resp.status_code == 200


# ── the security boundary that must still reject ──────────────────────────────


def test_non_whitelisted_command_still_rejected(dry_run_client):
    """The per-user whitelist is a security boundary. It is loaded in
    _prepare_execution, before _connect — so it must still reject in dry-run."""
    resp = _execute(dry_run_client, _auth(dry_run_client), command_name="rm_rf_root")
    assert resp.status_code in (403, 404)
    assert resp.status_code != 200


def test_argument_failing_validation_regex_still_rejected(dry_run_client):
    """`sleep` declares ^[0-9]+$ for its `time` argument."""
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client),
        command_name="sleep",
        arguments={"time": "not-a-number"},
    )
    assert resp.status_code != 200


def test_shell_metacharacters_still_rejected(dry_run_client):
    """The anti-injection check runs in _prepare_execution. Values are passed
    as positional arguments and never interpolated into a shell string, but the
    check is defence in depth and must not be lost in dry-run."""
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client),
        command_name="list_file",
        arguments={"key_word": "foo; rm -rf /"},
    )
    assert resp.status_code != 200


def test_valid_argument_still_accepted(dry_run_client):
    """The regex must reject bad input without rejecting good input."""
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client),
        command_name="list_file",
        arguments={"key_word": "README"},
    )
    assert resp.status_code == 200


# ── deny paths ────────────────────────────────────────────────────────────────


def test_missing_token_still_401(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/command/execution",
        json={
            "host": "node1",
            "host_type": "hostname",
            "username": "root",
            "port": 22,
            "command_name": "net_info",
        },
    )
    assert resp.status_code == 401


def test_missing_scope_still_403(dry_run_client):
    """test_deployer holds deploy_api but not command_api."""
    resp = _execute(dry_run_client, _auth(dry_run_client, "test_deployer"))
    assert resp.status_code == 403


def test_invalid_body_still_422(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/command/execution",
        headers=_auth(dry_run_client),
        json={"host": "node1"},
    )
    assert resp.status_code == 422
