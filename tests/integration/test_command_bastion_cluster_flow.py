"""End-to-end tests for the host_type=bastion → vm → mapping resolution."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import (
    get_bastion_mapping_repository,
    get_command_state_repository,
    get_inventory_repository,
    get_vm_repository,
)
from app.main import create_app
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryVmRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository
from tests.integration.test_command_host_type import (
    _InMemoryCommandStateRepo,
    _get_token,
    _patch_asyncssh,
)


def _vm_repo():
    return InMemoryVmRepository({
        "node1": VmInfo(id=1, name="node1",
                        k8s_cluster=VmK8sCluster(id=1, name="type1-cluster-c1")),
        "node2": VmInfo(id=2, name="node2",
                        k8s_cluster=VmK8sCluster(id=2, name="type2-cluster-c1")),
        "node3": VmInfo(id=3, name="node3",
                        k8s_cluster=VmK8sCluster(id=3, name="orphan-cluster")),
    })


def _mapping_repo():
    return InMemoryBastionMappingRepository({
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="r1", bastion="b1", bastion_ip="10.1.1.1",
            ),
            BastionMapping(
                pattern=["type1-kind"], runner="r2", bastion="b2",
                bastion_ip="10.1.1.2",
            ),
        ],
        "type2": [
            BastionMapping(
                pattern=["type2-cluster.*"], runner="r3", bastion="b3",
                bastion_ip="10.2.2.2",
            ),
        ],
    })


@pytest.fixture
def client_full():
    app = create_app()
    app.dependency_overrides[get_command_state_repository] = lambda: _InMemoryCommandStateRepo()
    app.dependency_overrides[get_inventory_repository] = lambda: InMemoryInventoryRepository({})
    app.dependency_overrides[get_vm_repository] = lambda: _vm_repo()
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


def test_default_bastion_type_used_when_option_omitted(client_full):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _post(client_full, host="node1")
        assert resp.status_code == 200, resp.text
        assert mock_connect.call_args.kwargs["host"] == "10.1.1.1"


def test_explicit_bastion_type_overrides_default(client_full):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _post(
            client_full, host="node2",
            option={"timeout_seconds": 30, "bastion_type": "type2"},
        )
        assert resp.status_code == 200, resp.text
        assert mock_connect.call_args.kwargs["host"] == "10.2.2.2"


def test_unknown_bastion_type_returns_404(client_full):
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
