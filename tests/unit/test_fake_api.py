"""Unit tests for the fake-api endpoints."""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def fake_app():
    # fake-api is not a package — load main.py by file path.
    root = Path(__file__).resolve().parents[2]  # deploy-service/
    sys.path.insert(0, str(root / "fake-api"))
    try:
        if "main" in sys.modules:
            del sys.modules["main"]
        module = importlib.import_module("main")
    finally:
        sys.path.pop(0)
    return TestClient(module.app)


def _auth():
    return {"Authorization": "Bearer test"}


def test_vms_known_node_returns_fixture(fake_app):
    r = fake_app.get("/api/v1/vms", params={"name": "node1"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["results"][0]["name"] == "node1"
    assert body["results"][0]["k8s-cluster"]["name"] == "type1-cluster-c1"


def test_vms_unknown_node_returns_empty(fake_app):
    r = fake_app.get(
        "/api/v1/vms", params={"name": "does-not-exist"}, headers=_auth()
    )
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_vms_missing_auth_returns_401(fake_app):
    r = fake_app.get("/api/v1/vms", params={"name": "node1"})
    assert r.status_code == 401


def test_mappings_known_type_returns_fixture(fake_app):
    r = fake_app.get(
        "/api/v1/bastion-cluster-mappings",
        params={"type": "type1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["bastion"] == "type1-bastion"


def test_mappings_unknown_type_returns_empty(fake_app):
    r = fake_app.get(
        "/api/v1/bastion-cluster-mappings",
        params={"type": "does-not-exist"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_mappings_missing_auth_returns_401(fake_app):
    r = fake_app.get(
        "/api/v1/bastion-cluster-mappings", params={"type": "type1"}
    )
    assert r.status_code == 401
