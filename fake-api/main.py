"""Standalone fake Inventory API for local development.

Run alongside deploy-service:
    make inventory-api
"""

from __future__ import annotations

import json
import pathlib
from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="Fake Inventory API")
_DATA_PATH = pathlib.Path(__file__).parent / "data" / "inventory.json"


@app.get("/inventory/hosts/{hostname}")
def lookup(hostname: str, authorization: str | None = Header(default=None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    records = json.loads(_DATA_PATH.read_text())
    for record in records:
        if record.get("hostname") == hostname:
            return record
    raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")
