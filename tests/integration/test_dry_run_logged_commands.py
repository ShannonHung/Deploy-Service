"""
tests/integration/test_dry_run_logged_commands.py

T6: the two command shapes that are hardest to exercise against real
infrastructure both run end to end in dry-run.

A `logged` command detaches its output to a file on the control_node and
announces that it reached exec with a two-line stderr handshake (PGID, then a
literal READY). Without the handshake the executor reports a start-up failure,
so this path is genuinely protocol-sensitive — it is not enough for the fake to
return "something plausible".

A `disconnects_ssh` command (reboot) is the single most dangerous endpoint in
the API: running it for real against a host to test it is exactly what nobody
wants to do. In dry-run it takes its real code path — including the
is_closed() detection that distinguishes "the host went down as expected" from
"the command returned without disconnecting" — while no host is ever contacted.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

# The async execution path persists state in Redis.
pytestmark = pytest.mark.e2e

_TERMINAL = {"success", "failed", "killed"}


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
        "command_name": "run_ansible",
        "arguments": {"playbook": "ping.yml"},
    }
    body.update(overrides)
    return client.post("/api/v1/command/execution", headers=headers, json=body)


def _poll(client, headers, command_id, attempts=50):
    for _ in range(attempts):
        time.sleep(0.2)
        data = client.get(
            f"/api/v1/command/execution/{command_id}", headers=headers
        ).json()["data"]
        if data["status"] in _TERMINAL:
            return data
    pytest.fail(f"{command_id} never reached a terminal state")


# ── logged commands ───────────────────────────────────────────────────────────


def test_logged_command_is_accepted(dry_run_client):
    resp = _execute(dry_run_client, _auth(dry_run_client))
    assert resp.status_code == 200
    assert resp.json()["data"]["command_id"]


def test_logged_command_reaches_success(dry_run_client):
    """The READY handshake is what this really asserts: without it the executor
    raises CommandExecutionException and the command ends `failed`."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    assert _poll(dry_run_client, headers, command_id)["status"] == "success"


def test_logged_command_marks_the_response_as_dry_run(dry_run_client):
    resp = _execute(dry_run_client, _auth(dry_run_client))
    assert resp.json()["dry_run"] is True


def test_run_id_is_injected_into_the_pipeline(dry_run_client):
    """`{run_id}` is resolved from a command_id generated BEFORE the pipeline is
    built. If that ordering regressed the placeholder would survive into the
    executed command, so assert the id actually reached the command line."""
    headers = _auth(dry_run_client)
    data = _execute(dry_run_client, headers).json()["data"]
    command_id = data["command_id"]

    final = _poll(dry_run_client, headers, command_id)
    assert "{run_id}" not in (final.get("exec_command") or "")
    assert command_id in (final.get("exec_command") or "")


def test_run_log_path_is_computed_and_stored(dry_run_client):
    """The log path is derived from the command_id and is what the trace reads.

    A missing path makes the trace return empty without erroring, so assert the
    content rather than just the status.
    """
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    trace = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}/trace/ui", headers=headers
    )
    assert trace.status_code == 200
    assert trace.json()["data"]["lines"]


# ── log viewer and trace ──────────────────────────────────────────────────────


def test_trace_returns_non_empty_log_content(dry_run_client):
    """A logged run writes nothing to the SSH channel, so an empty trace would
    be indistinguishable from a broken viewer."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    data = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}/trace/ui", headers=headers
    ).json()["data"]
    assert data["lines"]
    assert any("dry-run" in line["content_html"] for line in data["lines"])


def test_trace_polling_terminates(dry_run_client):
    """The viewer polls from next_byte_offset. If the offset were ignored the
    same bytes would be served forever and the viewer would never settle."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    first = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}/trace/ui", headers=headers
    ).json()["data"]
    second = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}/trace/ui",
        headers=headers,
        params={"byte_offset": first["next_byte_offset"]},
    ).json()["data"]
    assert not second["lines"]


def test_view_endpoint_serves_the_viewer_shell(dry_run_client):
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]

    resp = dry_run_client.get(f"/api/v1/command/execution/{command_id}/view")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_trace_ui_accepts_a_header_token(dry_run_client):
    """/trace/ui carries its own command_api-scoped auth — the HTML shell is
    unauthed, so this endpoint is the actual gate."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    resp = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}/trace/ui", headers=headers
    )
    assert resp.status_code == 200


def test_trace_ui_accepts_the_cookie_set_by_token(dry_run_client):
    """The viewer's JS fetch() cannot attach a Bearer header, so it relies on
    the same-origin cookie POST /token sets. _auth left that cookie on the
    client, so this call carries no header and still succeeds."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    resp = dry_run_client.get(f"/api/v1/command/execution/{command_id}/trace/ui")
    assert resp.status_code == 200


def test_trace_ui_without_a_token_is_rejected(dry_run_client):
    """Dry-run must not quietly open the viewer's data endpoint. Cookies are
    cleared because the fixture's client keeps the one POST /token set, which
    would otherwise authenticate this request and make the assertion vacuous."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]

    dry_run_client.cookies.clear()
    resp = dry_run_client.get(f"/api/v1/command/execution/{command_id}/trace/ui")
    assert resp.status_code in (401, 403)


# ── fire-and-forget ───────────────────────────────────────────────────────────


def test_reboot_succeeds_without_contacting_a_host(dry_run_client):
    """The most dangerous endpoint in the API, made safe to exercise."""
    resp = _execute(
        dry_run_client, _auth(dry_run_client), command_name="reboot", arguments={}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "success"


def test_reboot_returns_no_command_id(dry_run_client):
    """Fire-and-forget writes no CommandState, so there is nothing to poll —
    returning an id would imply a pollable run that does not exist."""
    resp = _execute(
        dry_run_client, _auth(dry_run_client), command_name="reboot", arguments={}
    )
    assert not resp.json()["data"].get("command_id")


def test_reboot_reports_the_expected_disconnect(dry_run_client):
    """Success here means `conn.is_closed()` was true after the run — the real
    dual-mode detection, not a canned 200."""
    resp = _execute(
        dry_run_client, _auth(dry_run_client), command_name="reboot", arguments={}
    )
    assert "dropped as expected" in resp.json()["data"]["message"]


def test_reboot_is_marked_as_dry_run(dry_run_client):
    resp = _execute(
        dry_run_client, _auth(dry_run_client), command_name="reboot", arguments={}
    )
    assert resp.json()["dry_run"] is True


# ── deny paths ────────────────────────────────────────────────────────────────
#
# Validation runs strictly before _connect, so dry-run must not have weakened
# any of it. A suite that only asserted the 200s above would report "security
# checks passing" while proving nothing.


def test_logged_command_rejects_a_bad_argument(dry_run_client):
    """The playbook regex is the whitelist's argument validation, still live."""
    resp = _execute(
        dry_run_client, _auth(dry_run_client), arguments={"playbook": "evil.yml"}
    )
    assert resp.status_code in (400, 422)


def test_logged_command_rejects_shell_metacharacters(dry_run_client):
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client),
        arguments={"playbook": "ping.yml; rm -rf /"},
    )
    assert resp.status_code in (400, 422)


def test_reboot_without_a_token_is_401(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/command/execution",
        json={
            "host": "node1",
            "host_type": "hostname",
            "username": "root",
            "port": 22,
            "command_name": "reboot",
        },
    )
    assert resp.status_code == 401


def test_reboot_without_scope_is_403(dry_run_client):
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client, "test_deployer"),
        command_name="reboot",
        arguments={},
    )
    assert resp.status_code == 403
