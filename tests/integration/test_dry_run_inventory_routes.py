"""
tests/integration/test_dry_run_inventory_routes.py

T3: the inventory endpoints answer with canned data when DRY_RUN_MODE=true,
with no reachable inventory API.

The inventory base URL is pointed at an unroutable host on purpose: if the
dry-run wiring were wrong, the request would attempt a real connection and
fail. That failure is the signal these tests exist to give.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    # Unreachable on purpose — nothing may call out.
    monkeypatch.setenv("INVENTORY_API_URL", "http://unreachable.invalid:1")
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── happy paths ───────────────────────────────────────────────────────────────


def test_node_lookup_returns_200_without_inventory_api(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert resp.json()["data"]["node"]["name"] == "node1"


def test_list_mappings_returns_200(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/inventory/mappings?type=type1", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]


def test_node_bastion_resolution_returns_200(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers=_auth(dry_run_client),
    )
    assert resp.status_code == 200


def test_cluster_bastion_resolution_returns_200(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/inventory/cluster/bastion-resolution?cluster_name=foo",
        headers=_auth(dry_run_client),
    )
    assert resp.status_code == 200


# ── the resolution logic that must still run ──────────────────────────────────


def test_node_type_mapping_still_resolved_from_config(dry_run_client):
    """bastion_type is derived from node_type via BASTION_NODE_TYPE_MAP. The
    canned node_type is 'baremetal', which .env.test maps to 'type1' — so
    seeing type1 with source 'config' proves the real mapping logic ran rather
    than a canned answer being echoed."""
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers=_auth(dry_run_client),
    )
    data = resp.json()["data"]
    assert data["node_type"] == "baremetal"
    assert data["bastion_type"] == "type1"
    assert data["bastion_type_source"] == "config"


def test_bastion_type_override_still_honoured(dry_run_client):
    """The query-param override path must keep working in dry-run."""
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution?bastion_type=type2",
        headers=_auth(dry_run_client),
    )
    data = resp.json()["data"]
    assert data["bastion_type"] == "type2"
    assert data["bastion_type_source"] == "query_param"


def test_pattern_matching_still_runs(dry_run_client):
    """A matched_pattern in the response means the regex matching executed."""
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers=_auth(dry_run_client),
    )
    assert resp.json()["data"]["matched_pattern"]


# ── deny paths must still hold ────────────────────────────────────────────────


def test_missing_token_still_401(dry_run_client):
    assert dry_run_client.get("/api/v1/inventory/nodes/node1").status_code == 401


def test_missing_scope_still_403(dry_run_client):
    """test_deployer holds deploy_api but not command_api."""
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1",
        headers=_auth(dry_run_client, "test_deployer"),
    )
    assert resp.status_code == 403


def test_missing_required_query_param_still_422(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/inventory/mappings", headers=_auth(dry_run_client)
    )
    assert resp.status_code == 422
