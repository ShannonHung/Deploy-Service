"""
tests/integration/test_dry_run_contract.py

T7: the assertions that make the whole dry-run design defensible.

Every other dry-run test file proves some path still *works*. This one proves
dry-run did not weaken anything — which is the point of the spec, not a tidying
exercise at the end of it.

The failure mode being guarded against is specific. A dry-run built by
short-circuiting at the router answers 200 to everything, so a test suite
written against it passes just as happily with auth deleted, the command
whitelist bypassed or the schema broken. Such a suite does not merely fail to
catch regressions: it reports "security checks passing" while proving nothing,
which is worse than having no e2e test at all. The seam was therefore placed
below every check (see docs/arch/dry-run-mode.md), and these tests are what
hold it there.

Deny paths in the per-ticket files cover their own endpoints. What lives here
is the cross-cutting contract: host restrictions, the response-format gate,
request-id propagation, the OpenAPI surface, and the dry_run marker itself.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

# The command endpoints persist state in Redis.
pytestmark = pytest.mark.e2e

_TERMINAL = {"success", "failed", "killed"}


def _client(monkeypatch, dry_run: bool) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true" if dry_run else "false")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    with _client(monkeypatch, dry_run=True) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def live_client(monkeypatch) -> TestClient:
    """DRY_RUN_MODE off. Used only for contract comparisons — no request made
    through it is allowed to reach a real GitLab, host or inventory."""
    with _client(monkeypatch, dry_run=False) as c:
        yield c
    get_settings.cache_clear()


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _command_body(**overrides) -> dict:
    body = {
        "host": "node1",
        "host_type": "hostname",
        "username": "root",
        "port": 22,
        "command_name": "net_info",
    }
    body.update(overrides)
    return body


def _execute(client, headers, **overrides):
    return client.post(
        "/api/v1/command/execution", headers=headers, json=_command_body(**overrides)
    )


def _poll(client, headers, command_id, attempts=50):
    for _ in range(attempts):
        time.sleep(0.2)
        data = client.get(
            f"/api/v1/command/execution/{command_id}", headers=headers
        ).json()["data"]
        if data["status"] in _TERMINAL:
            return data
    pytest.fail(f"{command_id} never reached a terminal state")


# ── host restrictions ─────────────────────────────────────────────────────────
#
# allow_hosts / deny_hosts are per-USER (top-level in the whitelist file), not
# per-command. The `test_command` fixture permits only 10.0.0.0/8 while the
# dry-run inventory resolves into RFC 5737 TEST-NET-1, so its requests clear the
# command whitelist and are then refused by the host check — which is the branch
# under test here.
#
# These match on the *resolved* IP, not the requested hostname, so they only
# mean anything because the dry-run inventory actually resolved something. A
# stub that skipped resolution would make them vacuous.


def test_host_outside_the_allow_list_is_rejected(dry_run_client):
    """The allow list fails closed — a host it does not name is refused rather
    than permitted by default. Dry-run must not make it reachable."""
    resp = _execute(
        dry_run_client, _auth(dry_run_client, "test_command"), command_name="net_info"
    )
    assert resp.status_code == 403


def test_host_rejection_names_the_resolved_ip(dry_run_client):
    """Guards the reason the test above is meaningful: the check ran against
    what the inventory resolved, so it cannot be dodged by varying the hostname
    spelling. If resolution had been skipped there would be no IP to report."""
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client, "test_command"),
        command_name="net_info",
        host="NODE1",
    )
    assert resp.status_code == 403
    assert "192.0.2." in resp.text


def test_an_unrestricted_user_is_not_blocked(dry_run_client):
    """The mirror case. Without it, a host check that rejected everything
    would satisfy the two tests above while being equally broken."""
    resp = _execute(dry_run_client, _auth(dry_run_client), command_name="net_info")
    assert resp.status_code == 200


# ── response-format gate ──────────────────────────────────────────────────────


def test_format_json_rejected_for_a_text_command(dry_run_client):
    """?format=json is only valid for a command whose whitelist entry declares
    output_format json. Dry-run must not relax the contract check."""
    headers = _auth(dry_run_client)
    command_id = _execute(
        dry_run_client, headers, command_name="list_file",
        arguments={"key_word": "etc"},
    ).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    resp = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}",
        headers=headers,
        params={"format": "json"},
    )
    assert resp.status_code == 400


def test_format_json_accepted_for_a_json_command(dry_run_client):
    """The negative above would also pass if ?format=json were rejected
    universally, so pin the positive case too."""
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]
    _poll(dry_run_client, headers, command_id)

    resp = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}",
        headers=headers,
        params={"format": "json"},
    )
    assert resp.status_code == 200


def test_unknown_format_value_is_422(dry_run_client):
    headers = _auth(dry_run_client)
    command_id = _execute(dry_run_client, headers).json()["data"]["command_id"]

    resp = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}",
        headers=headers,
        params={"format": "yaml"},
    )
    assert resp.status_code == 422


# ── request-id propagation ────────────────────────────────────────────────────


def test_coordination_id_is_echoed_as_request_id(dry_run_client):
    """X-Coordination-ID → request_id is how a caller correlates its pipeline
    run with our logs. An e2e test that could not correlate would be hard to
    diagnose precisely when it matters."""
    coordination_id = "e2e-dry-run-12345"
    resp = dry_run_client.get(
        "/api/v1/command/running",
        headers={**_auth(dry_run_client), "X-Coordination-ID": coordination_id},
    )
    assert resp.json()["request_id"] == coordination_id


def test_request_id_is_generated_when_the_header_is_absent(dry_run_client):
    resp = dry_run_client.get("/api/v1/command/running", headers=_auth(dry_run_client))
    assert resp.json()["request_id"]


def test_coordination_id_survives_a_rejected_request(dry_run_client):
    """Correlation matters most on failures — a 403 the caller cannot tie back
    to its run is the hardest kind to chase."""
    coordination_id = "e2e-dry-run-denied"
    resp = _execute(
        dry_run_client,
        {**_auth(dry_run_client), "X-Coordination-ID": coordination_id},
        command_name="rm_rf_slash",
    )
    assert resp.status_code in (403, 404)
    assert coordination_id in resp.text


# ── the dry_run marker ────────────────────────────────────────────────────────


def test_responses_are_marked_dry_run(dry_run_client):
    resp = dry_run_client.get("/api/v1/command/running", headers=_auth(dry_run_client))
    assert resp.json()["dry_run"] is True


def test_responses_are_not_marked_when_the_mode_is_off(live_client):
    """The marker has to be a real signal. If it were true unconditionally a
    caller could never tell a dry-run deployment from a production one, which
    is the single most dangerous confusion this feature could cause."""
    resp = live_client.get("/api/v1/command/running", headers=_auth(live_client))
    assert resp.json()["dry_run"] is False


def test_every_dry_run_endpoint_carries_the_marker(dry_run_client):
    """The marker is a response-model default, so a route that built its
    response differently would silently omit it."""
    headers = _auth(dry_run_client)
    for path in (
        "/api/v1/command/running",
        "/api/v1/inventory/mappings",
    ):
        resp = dry_run_client.get(path, headers=headers)
        if resp.status_code == 200:
            assert resp.json().get("dry_run") is True, path


# ── OpenAPI surface ───────────────────────────────────────────────────────────


def test_openapi_paths_are_identical_in_both_modes(dry_run_client, live_client):
    """Dry-run must not add, remove or rename an endpoint. A caller writing
    against the dry-run instance has to be writing against the real contract."""
    dry = dry_run_client.get("/openapi.json").json()
    live = live_client.get("/openapi.json").json()
    assert set(dry["paths"]) == set(live["paths"])


def test_openapi_methods_are_identical_in_both_modes(dry_run_client, live_client):
    dry = dry_run_client.get("/openapi.json").json()["paths"]
    live = live_client.get("/openapi.json").json()["paths"]
    assert {p: sorted(ops) for p, ops in dry.items()} == {
        p: sorted(ops) for p, ops in live.items()
    }


def test_openapi_schemas_are_identical_in_both_modes(dry_run_client, live_client):
    """Covers the response models themselves — a dry-run-only field would be a
    contract divergence even if every path matched."""
    dry = dry_run_client.get("/openapi.json").json()["components"]["schemas"]
    live = live_client.get("/openapi.json").json()["components"]["schemas"]
    assert dry == live


def test_security_requirements_are_unchanged(dry_run_client, live_client):
    """The scopes declared on each operation are part of the published
    contract; dry-run relaxing one would be invisible in a 200-only suite."""

    def _security(spec):
        return {
            (path, method): op.get("security")
            for path, ops in spec["paths"].items()
            for method, op in ops.items()
        }

    assert _security(dry_run_client.get("/openapi.json").json()) == _security(
        live_client.get("/openapi.json").json()
    )


# ── auth, across both services' endpoint families ─────────────────────────────
#
# The per-ticket files assert these per endpoint. Repeating the matrix here is
# deliberate: it is the single place that fails loudly if a future change makes
# dry-run skip authentication globally.


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/command/running"),
        ("post", "/api/v1/command/execution"),
        ("get", "/api/v1/inventory/mappings"),
        ("post", "/api/v1/deploy/stage"),
    ],
)
def test_no_token_is_401_everywhere(dry_run_client, method, path):
    resp = getattr(dry_run_client, method)(path)
    assert resp.status_code == 401, f"{method.upper()} {path}"


def test_wrong_scope_is_403_for_command_endpoints(dry_run_client):
    """test_deployer holds deploy_api but not command_api.

    Asserting the *reason* matters here. test_deployer also has no command
    whitelist file, so if scope enforcement were bypassed the request would
    still 403 — from the whitelist loader instead. A bare status assertion
    would pass either way and quietly stop testing scopes at all.
    """
    resp = _execute(dry_run_client, _auth(dry_run_client, "test_deployer"))
    assert resp.status_code == 403
    assert "scope" in resp.text.lower()


def test_wrong_scope_is_403_for_deploy_endpoints(dry_run_client):
    """test_command holds command_api but not deploy_api — the mirror of the
    case above, so neither scope can be the one that silently stopped being
    enforced."""
    resp = dry_run_client.post(
        "/api/v1/deploy/stage",
        headers=_auth(dry_run_client, "test_command"),
        params={"action": "deploy"},
        json={},
    )
    assert resp.status_code == 403


def test_a_malformed_token_is_rejected(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/command/running",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


# ── validation, end to end ────────────────────────────────────────────────────


def test_non_whitelisted_command_is_refused(dry_run_client):
    """The per-user whitelist is the primary authorisation check for the
    command API and must survive dry-run intact."""
    resp = _execute(dry_run_client, _auth(dry_run_client), command_name="rm_rf_slash")
    assert resp.status_code in (403, 404)


def test_malformed_body_is_422(dry_run_client):
    resp = dry_run_client.post(
        "/api/v1/command/execution",
        headers=_auth(dry_run_client),
        json={"host": "node1"},
    )
    assert resp.status_code == 422


def test_argument_regex_is_enforced(dry_run_client):
    resp = _execute(
        dry_run_client,
        _auth(dry_run_client),
        command_name="list_file",
        arguments={"key_word": "etc; rm -rf /"},
    )
    assert resp.status_code in (400, 422)


def test_undeclared_argument_never_reaches_the_command_line(dry_run_client):
    """An argument the whitelist never declared is ignored, not interpolated.

    Substitution is driven by the whitelist's own argument definitions, so an
    extra key has nothing to bind to — the request is accepted and the value
    simply never appears. That is the guarantee worth pinning: the attack
    surface is the declared arguments, and an unexpected key cannot widen it.
    """
    headers = _auth(dry_run_client)
    resp = _execute(
        dry_run_client,
        headers,
        command_name="list_file",
        arguments={"key_word": "etc", "injected": "PAYLOAD-SHOULD-NOT-APPEAR"},
    )
    assert resp.status_code == 200

    final = _poll(dry_run_client, headers, resp.json()["data"]["command_id"])
    assert "PAYLOAD-SHOULD-NOT-APPEAR" not in (final.get("exec_command") or "")
