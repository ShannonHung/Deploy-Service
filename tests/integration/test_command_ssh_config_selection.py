"""SSH config selection through the HTTP surface (ADR 0002).

The fixture SSH-default.json is materialised by tests/conftest.py; asyncssh is
stubbed so a request that gets past SSH config selection reaches _connect.
"""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.dependencies import (
    get_command_state_repository,
    get_inventory_repository,
)
from app.main import create_app
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    ClusterRef,
    NodeInfo,
)
from app.services import command_ssh as ssh_mod
from tests.fixtures.cluster import InMemoryInventoryRepository
from tests.integration.test_command_host_type import (
    _InMemoryCommandStateRepo,
    _get_token,
    _patch_asyncssh,
)


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_command_state_repository] = (
        lambda: _InMemoryCommandStateRepo()
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _execute(client, **overrides):
    body = {
        "command_name": "list_file",
        "host": "10.0.99.99",
        "host_type": "ip",
        "username": "root",
        "arguments": {"key_word": "ssh"},
        **overrides,
    }
    token = _get_token(client)
    return client.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


@pytest.mark.parametrize("bad", ["../default", "a/b", "..", "", "x y", "x;y"])
def test_unsafe_ssh_config_name_is_rejected_422(client, bad):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _execute(client, ssh_config=bad)
    assert resp.status_code == 422, resp.text
    mock_connect.assert_not_called()


def test_unknown_explicit_ssh_config_is_400_not_silent_fallback(client):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _execute(client, ssh_config="no-such-bastion")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["detail"] == {"ssh_config": "no-such-bastion"}
    mock_connect.assert_not_called()


def test_default_ssh_config_still_works(client):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        resp = _execute(client)
    assert resp.status_code == 200, resp.text
    mock_connect.assert_called_once()


# ── Auto-selection for bastion / cluster hosts (#50) ─────────────────────────

_FIXTURE_DEFAULT = Path(__file__).resolve().parents[1] / "fixtures" / "SSH-default.json"


def _inventory(bastion_name="b1"):
    return InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.0.1.5/8"}),
                cluster=ClusterRef(id="1", name="type1-cluster-c1"),
            )
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["type1-.*"],
                    runner="r1",
                    bastion=bastion_name,
                    bastion_ip="10.1.1.1",
                )
            ]
        },
    )


@pytest.fixture
def ssh_dir(monkeypatch, tmp_path):
    """An SSH config dir holding only the Fallback SSH config."""
    shutil.copy(_FIXTURE_DEFAULT, tmp_path / "SSH-default.json")
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _bastion_client(inventory):
    app = create_app()
    state_repo = _InMemoryCommandStateRepo()
    app.dependency_overrides[get_command_state_repository] = lambda: state_repo
    app.dependency_overrides[get_inventory_repository] = lambda: inventory
    return app, state_repo


def _recorded_ssh_config(state_repo):
    (state,) = state_repo._store.values()
    return state.ssh_config


@pytest.mark.parametrize(
    "host,host_type", [("node1", "bastion"), ("type1-cluster-c1", "cluster")]
)
def test_default_selects_the_provisioned_bastion_ssh_config(ssh_dir, host, host_type):
    shutil.copy(ssh_dir / "SSH-default.json", ssh_dir / "SSH-b1.json")
    app, state_repo = _bastion_client(_inventory())
    p, _ = _patch_asyncssh()
    with TestClient(app) as c, p as mock_connect:
        resp = _execute(c, host=host, host_type=host_type)
        assert resp.status_code == 200, resp.text
        mock_connect.assert_called_once()
        assert _recorded_ssh_config(state_repo) == "b1"

        command_id = resp.json()["data"]["command_id"]
        token = _get_token(c)
        got = c.get(
            f"/api/v1/command/execution/{command_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert got.json()["data"]["ssh_config"] == "b1"


def test_default_falls_back_when_bastion_has_no_ssh_config(ssh_dir):
    app, state_repo = _bastion_client(_inventory())
    p, _ = _patch_asyncssh()
    with TestClient(app) as c, p:
        resp = _execute(c, host="node1", host_type="bastion")
    assert resp.status_code == 200, resp.text
    assert _recorded_ssh_config(state_repo) == "default"


def test_explicit_ssh_config_on_a_bastion_host_is_not_overridden(ssh_dir):
    shutil.copy(ssh_dir / "SSH-default.json", ssh_dir / "SSH-b1.json")
    shutil.copy(ssh_dir / "SSH-default.json", ssh_dir / "SSH-cluster1.json")
    app, state_repo = _bastion_client(_inventory())
    p, _ = _patch_asyncssh()
    with TestClient(app) as c, p:
        resp = _execute(c, host="node1", host_type="bastion", ssh_config="cluster1")
    assert resp.status_code == 200, resp.text
    assert _recorded_ssh_config(state_repo) == "cluster1"


def test_unsafe_bastion_name_from_inventory_is_500_and_never_connects(ssh_dir):
    app, _ = _bastion_client(_inventory(bastion_name="../../etc/x"))
    p, _ = _patch_asyncssh()
    with TestClient(app) as c, p as mock_connect:
        resp = _execute(c, host="node1", host_type="bastion")
    assert resp.status_code == 500, resp.text
    mock_connect.assert_not_called()


def test_dry_run_still_validates_the_bastion_name(monkeypatch, ssh_dir):
    """Selection sits above the dry-run seam (only the file read is faked)."""
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    try:
        app, _ = _bastion_client(_inventory(bastion_name="../../etc/x"))
        with TestClient(app) as c:
            resp = _execute(c, host="node1", host_type="bastion")
        assert resp.status_code == 500, resp.text
    finally:
        get_settings.cache_clear()
