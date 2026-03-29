import pytest
from fastapi.testclient import TestClient

def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]

def test_execute_list_file(client: TestClient):
    token = _get_token(client, "test_admin")
    resp = client.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "list_file",
            "host": "localhost",
            "port": 2222,
            "username": "root",
            "ssh_config": "cluster1",
            "option": {"timeout_seconds": 10},
            "arguments": {"key_word": "ssh"}
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    print("DEBUG RESPONSE:", data)
    assert data["status"] == "running"
    assert "command_id" in data

def test_execute_invalid_argument_regex(client: TestClient):
    token = _get_token(client, "test_admin")
    resp = client.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "sleep",
            "host": "localhost",
            "port": 2222,
            "username": "root",
            "arguments": {"time": "notanint"}
        }
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert "does not match validation regex" in data["message"]

def test_execute_reboot_fire_and_forget(client: TestClient):
    token = _get_token(client, "test_admin")
    resp = client.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "reboot",
            "host": "localhost",
            "port": 2222,
            "username": "root",
            "ssh_config": "cluster1",
            "arguments": {}
        }
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert "System has not been booted with systemd" in data["output"]
