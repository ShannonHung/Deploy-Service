"""Standalone fake Inventory + Cluster API for local development.

Run alongside deploy-service:
    make inventory-api
"""

from __future__ import annotations

import json
import pathlib
from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="Fake Inventory + Cluster API")
_DATA_DIR = pathlib.Path(__file__).parent / "data"
_INVENTORY_PATH = _DATA_DIR / "inventory.json"


def _require_auth(authorization: str | None) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")


def _read_with_fallback(prefix: str, key: str) -> dict:
    """Return JSON from data/{prefix}-{key}.json, falling back to {prefix}-not-found.json."""
    # Dev-only fake API: no path-traversal guard. Never run this in prod.
    specific = _DATA_DIR / f"{prefix}-{key}.json"
    fallback = _DATA_DIR / f"{prefix}-not-found.json"
    target = specific if specific.is_file() else fallback
    return json.loads(target.read_text())


@app.get("/inventory/hosts/{hostname}")
def lookup_inventory(hostname: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    records = json.loads(_INVENTORY_PATH.read_text())
    for record in records:
        if record.get("hostname") == hostname:
            return record
    raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")


@app.get("/api/v1/vms")
def list_vms(name: str, authorization: str | None = Header(default=None)):
    """Return vms matching {name}. Always 200; unknown → empty results."""
    _require_auth(authorization)
    return _read_with_fallback("vms", name)


@app.get("/api/v1/bastion-cluster-mappings")
def list_bastion_cluster_mappings(
    type: str, authorization: str | None = Header(default=None)
):
    """Return bastion-cluster mappings for {type}. Always 200; unknown → empty results."""
    _require_auth(authorization)
    return _read_with_fallback("bastion-cluster-mappings", type)
