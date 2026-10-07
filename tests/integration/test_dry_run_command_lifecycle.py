"""
tests/integration/test_dry_run_command_lifecycle.py

T5: the async command lifecycle — immediate command_id, polling to a terminal
state, and the kill transition — runs for real in dry-run.

The state machine is genuinely executing here, not canned: because the seam is
_connect, _handle_async_execution runs unchanged, so Redis persistence and the
RUNNING → KILLING → KILLED transitions are the production code paths.

Killing needs a command that is still running when the request arrives. A
dry-run command otherwise finishes instantly and is already terminal, so
DRY_RUN_COMMAND_SECONDS gives the fake run a measurable duration.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

# State is persisted in Redis.
pytestmark = pytest.mark.e2e

_TERMINAL = {"success", "failed", "killed"}


def _make_client(monkeypatch, run_seconds: str = "0") -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("DRY_RUN_COMMAND_SECONDS", run_seconds)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    """Commands finish immediately — for poll-to-completion assertions."""
    with _make_client(monkeypatch) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def slow_dry_run_client(monkeypatch) -> TestClient:
    """Commands stay running long enough to be killed."""
    with _make_client(monkeypatch, run_seconds="3") as c:
        yield c
    get_settings.cache_clear()


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _start(client, headers, command_name="net_info", **extra):
    body = {
        "host": "node1",
        "host_type": "hostname",
        "username": "root",
        "port": 22,
        "command_name": command_name,
    }
    body.update(extra)
    return client.post("/api/v1/command/execution", headers=headers, json=body)


def _poll(client, headers, command_id, until=_TERMINAL, attempts=50):
    for _ in range(attempts):
        time.sleep(0.2)
        data = client.get(
            f"/api/v1/command/execution/{command_id}", headers=headers
        ).json()["data"]
        if data["status"] in until:
            return data
    pytest.fail(f"{command_id} never reached {until}")


# ── async contract ────────────────────────────────────────────────────────────


def test_execute_returns_command_id_immediately(dry_run_client):
    headers = _auth(dry_run_client)
    data = _start(dry_run_client, headers).json()["data"]
    assert data["command_id"]
    assert data["status"] == "running"


def test_poll_reaches_a_terminal_state(dry_run_client):
    """A poll loop that never terminates would hang an e2e suite."""
    headers = _auth(dry_run_client)
    command_id = _start(dry_run_client, headers).json()["data"]["command_id"]
    assert _poll(dry_run_client, headers, command_id)["status"] == "success"


def test_polling_a_finished_command_is_stable(dry_run_client):
    """The stored result must survive repeated reads."""
    headers = _auth(dry_run_client)
    command_id = _start(dry_run_client, headers).json()["data"]["command_id"]
    first = _poll(dry_run_client, headers, command_id)
    again = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}", headers=headers
    ).json()["data"]
    assert again["status"] == first["status"]


def test_unknown_command_id_is_404(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/command/execution/no-such-id", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 404


def test_running_list_endpoint_works(dry_run_client):
    resp = dry_run_client.get("/api/v1/command/running", headers=_auth(dry_run_client))
    assert resp.status_code == 200


# ── state machine ─────────────────────────────────────────────────────────────


def test_state_is_running_before_completion(slow_dry_run_client):
    """Proves the RUNNING state is actually persisted rather than the command
    jumping straight to a terminal state."""
    headers = _auth(slow_dry_run_client)
    command_id = _start(
        slow_dry_run_client, headers, command_name="sleep", arguments={"time": "60"}
    ).json()["data"]["command_id"]

    time.sleep(0.4)
    data = slow_dry_run_client.get(
        f"/api/v1/command/execution/{command_id}", headers=headers
    ).json()["data"]
    assert data["status"] == "running"


def test_kill_transitions_running_command_to_killed(slow_dry_run_client):
    """The real RUNNING → KILLING → KILLED path, including the two-phase
    PGID kill over the fake connection."""
    headers = _auth(slow_dry_run_client)
    command_id = _start(
        slow_dry_run_client, headers, command_name="sleep", arguments={"time": "60"}
    ).json()["data"]["command_id"]

    time.sleep(0.4)
    kill = slow_dry_run_client.post(
        f"/api/v1/command/execution/{command_id}/kill", headers=headers
    )
    assert kill.status_code == 200

    final = _poll(slow_dry_run_client, headers, command_id)
    assert final["status"] == "killed"


def test_killing_a_finished_command_is_409(dry_run_client):
    """Kill is only valid from RUNNING; a finished command must be refused
    rather than silently re-killed."""
    headers = _auth(dry_run_client)
    command_id = _start(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    resp = dry_run_client.post(
        f"/api/v1/command/execution/{command_id}/kill", headers=headers
    )
    assert resp.status_code == 409


def test_killing_unknown_command_is_404(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/command/execution/no-such-id/kill", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 404


# ── deny paths ────────────────────────────────────────────────────────────────


def test_kill_without_token_is_401(dry_run_client):
    assert (
        dry_run_client.post("/api/v1/command/execution/whatever/kill").status_code
        == 401
    )


def test_kill_without_scope_is_403(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/command/execution/whatever/kill",
        headers=_auth(dry_run_client, "test_deployer"),
    )
    assert resp.status_code == 403
