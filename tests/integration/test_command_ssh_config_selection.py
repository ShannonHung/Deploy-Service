"""SSH config selection through the HTTP surface (ADR 0002).

The fixture SSH-default.json is materialised by tests/conftest.py; asyncssh is
stubbed so a request that gets past SSH config selection reaches _connect.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_command_state_repository
from app.main import create_app
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
