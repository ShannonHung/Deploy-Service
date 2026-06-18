"""End-to-end tests for the host_type=bastion → cluster-node-lookup → mapping resolution."""

import pytest
from fastapi.testclient import TestClient

import app.services.command_service as svc_module
from app.core.dependencies import (
    get_bastion_mapping_repository,
    get_cluster_node_lookup_repository,
    get_command_state_repository,
    get_inventory_repository,
)
from app.main import create_app
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.cluster_node_lookup_repository import ClusterNodeInfo, ClusterRef
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryClusterNodeLookupRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository
from tests.integration.test_command_host_type import (
    _InMemoryCommandStateRepo,
    _get_token,
    _patch_asyncssh,
)


def _cluster_node_lookup_repo():
    return InMemoryClusterNodeLookupRepository({
        "node1": ClusterNodeInfo(
            node_type="baremetal", node_name="node1",
            cluster=ClusterRef(id="1", name="type1-cluster-c1"),
        ),
        "node2": ClusterNodeInfo(
            # virtual-machine → maps to type2 via BASTION_NODE_TYPE_MAP
            node_type="virtual-machine", node_name="node2",
            cluster=ClusterRef(id="2", name="type2-cluster-c1"),
        ),
        "node3": ClusterNodeInfo(
            node_type="baremetal", node_name="node3",
            cluster=ClusterRef(id="3", name="orphan-cluster"),
        ),
    })


def _mapping_repo():
    return InMemoryBastionMappingRepository({
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="r1", bastion="b1", bastion_ip="10.1.1.1",
            ),
            BastionMapping(
                patterns=["type1-kind"], runner="r2", bastion="b2",
                bastion_ip="10.1.1.2",
            ),
        ],
        "type2": [
            BastionMapping(
                patterns=["type2-cluster.*"], runner="r3", bastion="b3",
                bastion_ip="10.2.2.2",
            ),
        ],
    })


@pytest.fixture
def client_full(monkeypatch):
    monkeypatch.setattr(
        svc_module.settings,
        "BASTION_NODE_TYPE_MAP",
        {"baremetal": "type1", "virtual-machine": "type2"},
    )
    app = create_app()
    app.dependency_overrides[get_command_state_repository] = lambda: _InMemoryCommandStateRepo()
    app.dependency_overrides[get_inventory_repository] = lambda: InMemoryInventoryRepository({})
    app.dependency_overrides[get_cluster_node_lookup_repository] = lambda: _cluster_node_lookup_repo()
    app.dependency_overrides[get_bastion_mapping_repository] = lambda: _mapping_repo()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _post(client, *, host, option=None):
    token = _get_token(client)
    body = {
        "command_name": "list_file",
        "host": host,
        "host_type": "bastion",
        "port": 22,
        "username": "root",
        "arguments": {"key_word": "ssh"},
    }
    if option is not None:
        body["option"] = option
    return client.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


def test_node_type_map_selects_bastion_automatically(client_full):
    """baremetal node → BASTION_NODE_TYPE_MAP picks type1 → connects to type1 bastion IP."""
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _post(client_full, host="node1")
        assert resp.status_code == 200, resp.text
        assert mock_connect.call_args.kwargs["host"] == "10.1.1.1"


def test_node_type_map_selects_different_type_for_virtual_machine(client_full):
    """virtual-machine node → BASTION_NODE_TYPE_MAP picks type2 → connects to type2 bastion IP."""
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _post(client_full, host="node2")
        assert resp.status_code == 200, resp.text
        assert mock_connect.call_args.kwargs["host"] == "10.2.2.2"


def test_explicit_bastion_type_in_option_overrides_node_type_map(client_full):
    """bastion_type in option overrides node_type_map; node1 (baremetal→type1) forced to type2."""
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _post(
            client_full, host="node2",
            option={"timeout_seconds": 30, "bastion_type": "type2"},
        )
        assert resp.status_code == 200, resp.text
        assert mock_connect.call_args.kwargs["host"] == "10.2.2.2"


def test_unknown_bastion_type_in_option_returns_404(client_full):
    resp = _post(
        client_full, host="node1",
        option={"timeout_seconds": 30, "bastion_type": "no-such-type"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_no_pattern_matches_returns_404(client_full):
    resp = _post(client_full, host="node3")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "orphan-cluster" in str(body["error"])


def test_unknown_node_returns_404(client_full):
    resp = _post(client_full, host="ghost-node")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
