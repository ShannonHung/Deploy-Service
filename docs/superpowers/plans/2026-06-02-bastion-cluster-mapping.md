# Bastion Cluster Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `host_type=bastion` resolution with a two-step `vms` → `bastion-cluster-mappings` lookup (regex priority match) while keeping the public HTTP contract for `POST /api/v1/command/execution` backwards compatible.

**Architecture:** Two new fake-API endpoints back two new repository pairs (`VmRepository`, `BastionMappingRepository`). A new `ClusterBastionHostResolver` replaces `BastionHostResolver`. `CommandOption.bastion_type` (optional) drives which mapping set is used, defaulting to `BASTION_DEFAULT_TYPE` from settings.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, httpx (`MockTransport` for tests), pytest (`asyncio_mode = "auto"`), uv.

**Spec:** `deploy-service/docs/superpowers/specs/2026-06-02-bastion-cluster-mapping-design.md`

**Working directory for all commands:** `deploy-service/`

---

## File Map

**Create:**

- `app/repositories/vm_repository.py` — `VmRepository` ABC + `HttpVmRepository`
- `app/repositories/bastion_mapping_repository.py` — `BastionMappingRepository` ABC + `HttpBastionMappingRepository`
- `tests/fixtures/cluster.py` — in-memory `VmRepository` + `BastionMappingRepository` for tests
- `tests/unit/test_vm_repository.py`
- `tests/unit/test_bastion_mapping_repository.py`
- `tests/unit/test_cluster_bastion_resolver.py`
- `tests/unit/test_fake_api.py` (extend if exists)
- `tests/integration/test_command_bastion_cluster_flow.py`

**Modify:**

- `fake-api/main.py` — add two endpoints
- `fake-api/data/bastion-cluster-mappings-type1.json` — `*` → `.*` in patterns
- `fake-api/data/bastion-cluster-mappings-type2.json` — `*` → `.*` in patterns
- `app/core/config.py` — four new settings
- `.env.dev`, `.env.test` — new env vars
- `app/domain/command.py` — `CommandOption.bastion_type`
- `app/repositories/host_resolver.py` — delete `BastionHostResolver`, add `ClusterBastionHostResolver`, update factory signature
- `app/core/dependencies.py` — two new DI factories; `get_command_service` injects them
- `app/services/command_service.py` — `__init__` takes new deps; `_prepare_execution` computes `bastion_type` and passes through factory
- `tests/unit/test_host_resolver.py` — drop old `BastionHostResolver` tests, replace with cluster-bastion tests
- `tests/integration/test_command_host_type.py` — `test_host_type_bastion_*` switched to new resolver path
- `ssh-command.md` — document new resolution chain

---

## Task 1: Fake-API — fix fixture regex patterns

**Files:**

- Modify: `fake-api/data/bastion-cluster-mappings-type1.json`
- Modify: `fake-api/data/bastion-cluster-mappings-type2.json`

The current `"type1-cluster*"` is a regex (zero-or-more `r`), not a glob. Under `re.fullmatch` it does not match `type1-cluster-c1`. Fix to `"type1-cluster.*"`.

- [ ] **Step 1: Patch type1 fixture**

Edit `fake-api/data/bastion-cluster-mappings-type1.json`. Change `"type1-cluster*"` to `"type1-cluster.*"`. Final content:

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "pattern": ["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
      "runner": "type1-runner",
      "bastion": "type1-bastion",
      "bastion_ip": "10.223.192.40"
    },
    {
      "pattern": ["type1-kind"],
      "runner": "type1-kind-runner",
      "bastion": "type1-kind-bastion",
      "bastion_ip": "10.223.192.40"
    }
  ]
}
```

- [ ] **Step 2: Patch type2 fixture**

Edit `fake-api/data/bastion-cluster-mappings-type2.json`. Change `"type2-cluster*"` to `"type2-cluster.*"`. Final content:

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "pattern": ["type2-cluster-(c1|c2|c3)", "type2-cluster.*"],
      "runner": "type2-runner",
      "bastion": "type2-bastion",
      "bastion_ip": "10.223.192.40"
    },
    {
      "pattern": ["type2-kind"],
      "runner": "type2-kind-runner",
      "bastion": "type2-kind-bastion",
      "bastion_ip": "10.223.192.40"
    }
  ]
}
```

- [ ] **Step 3: Commit**

```bash
git add fake-api/data/bastion-cluster-mappings-type1.json fake-api/data/bastion-cluster-mappings-type2.json
git commit -m "fix(fake-api): use regex .* not glob * in bastion mapping fixtures"
```

---

## Task 2: Fake-API — add `/api/v1/vms` and `/api/v1/bastion-cluster-mappings`

**Files:**

- Modify: `fake-api/main.py`
- Create: `tests/unit/test_fake_api.py`

Both endpoints follow the same pattern: read `data/<prefix>-<key>.json` if present, else `<prefix>-not-found.json`; require an `Authorization` header.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_fake_api.py`:

```python
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
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
APP_ENV=test uv run pytest tests/unit/test_fake_api.py -v
```

Expected: all six fail because the endpoints do not exist yet.

- [ ] **Step 3: Implement the endpoints**

Replace the contents of `fake-api/main.py` with:

```python
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
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
APP_ENV=test uv run pytest tests/unit/test_fake_api.py -v
```

Expected: all six pass.

- [ ] **Step 5: Commit**

```bash
git add fake-api/main.py tests/unit/test_fake_api.py
git commit -m "feat(fake-api): add /api/v1/vms and /api/v1/bastion-cluster-mappings"
```

---

## Task 3: Settings — add cluster API config

**Files:**

- Modify: `app/core/config.py`
- Modify: `.env.dev`
- Modify: `.env.test`

Settings are loaded by Pydantic; the test harness depends on the config loading cleanly. Add the four new settings with sensible defaults so a missing env var does not crash startup.

- [ ] **Step 1: Add settings**

Edit `app/core/config.py`. After the `# ── Inventory API ───` block, add:

```python
    # ── Cluster / Bastion mapping API ─────────────────────────────────────────
    CLUSTER_API_URL: str = "http://localhost:9001"
    CLUSTER_API_TOKEN: str = "fake-cluster-token"
    CLUSTER_API_TIMEOUT_SECONDS: float = 5.0
    BASTION_DEFAULT_TYPE: str = "type1"
```

- [ ] **Step 2: Update `.env.dev`**

Append to `.env.dev`:

```env

# Cluster / Bastion mapping API (same fake-api process as inventory)
CLUSTER_API_URL=http://localhost:9001
CLUSTER_API_TOKEN=fake-cluster-token
CLUSTER_API_TIMEOUT_SECONDS=5
BASTION_DEFAULT_TYPE=type1
```

- [ ] **Step 3: Update `.env.test`**

Append to `.env.test`:

```env

# Cluster / Bastion mapping API — tests override via DI; config must still load
CLUSTER_API_URL=http://localhost:9001
CLUSTER_API_TOKEN=test-cluster-token
CLUSTER_API_TIMEOUT_SECONDS=1
BASTION_DEFAULT_TYPE=type1
```

- [ ] **Step 4: Verify config loads**

```bash
APP_ENV=test uv run python -c "from app.core.config import get_settings; s = get_settings(); print(s.CLUSTER_API_URL, s.BASTION_DEFAULT_TYPE)"
```

Expected output: `http://localhost:9001 type1`

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py .env.dev .env.test
git commit -m "feat(config): add CLUSTER_API_* settings and BASTION_DEFAULT_TYPE"
```

---

## Task 4: `VmRepository` ABC + HTTP implementation

**Files:**

- Create: `app/repositories/vm_repository.py`
- Create: `tests/unit/test_vm_repository.py`

The repo abstracts the `/api/v1/vms` HTTP call. The invariant from spec §3.1 — "results length >1 means upstream is wrong" — must raise `UpstreamUnavailableException`.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_vm_repository.py`:

```python
import httpx
import pytest

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.vm_repository import (
    HttpVmRepository,
    VmInfo,
    VmK8sCluster,
)


def _repo(handler) -> HttpVmRepository:
    transport = httpx.MockTransport(handler)
    return HttpVmRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


async def test_lookup_success_returns_vm_info():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/vms"
        assert dict(request.url.params) == {"name": "node1"}
        assert request.headers.get("authorization") == "Bearer t"
        return httpx.Response(
            200,
            json={
                "count": 1,
                "results": [
                    {
                        "id": 123,
                        "name": "node1",
                        "k8s-cluster": {"id": 9, "name": "type1-cluster-c1"},
                    }
                ],
            },
        )

    repo = _repo(handler)
    info = await repo.lookup_by_name("node1")
    assert info == VmInfo(
        id=123,
        name="node1",
        k8s_cluster=VmK8sCluster(id=9, name="type1-cluster-c1"),
    )


async def test_lookup_empty_results_raises_not_found():
    repo = _repo(lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    with pytest.raises(NotFoundException):
        await repo.lookup_by_name("missing")


async def test_lookup_multiple_results_raises_upstream_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 2,
                "results": [
                    {"id": 1, "name": "node1", "k8s-cluster": {"id": 1, "name": "c1"}},
                    {"id": 2, "name": "node1", "k8s-cluster": {"id": 2, "name": "c2"}},
                ],
            },
        )

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("node1")


async def test_lookup_500_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.lookup_by_name("x")


async def test_lookup_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
APP_ENV=test uv run pytest tests/unit/test_vm_repository.py -v
```

Expected: ImportError because `app.repositories.vm_repository` does not exist.

- [ ] **Step 3: Implement the repository**

Create `app/repositories/vm_repository.py`:

```python
"""VM API repository.

GET {base_url}/api/v1/vms?name={node_name}

Contract:
  - 200 + exactly 1 result → VmInfo
  - 200 + empty results    → NotFoundException
  - 200 + >1 results       → UpstreamUnavailableException
                             (invariant violation: name lookup must be unique)
  - timeout                → UpstreamTimeoutException
  - other 4xx / 5xx / net  → UpstreamUnavailableException
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


class VmK8sCluster(BaseModel):
    id: int
    name: str


class VmInfo(BaseModel):
    # Allow both alias ("k8s-cluster" from JSON) and attr name ("k8s_cluster")
    # construction. The alias matches the upstream JSON key which contains a
    # hyphen and is therefore not a valid Python identifier.
    model_config = ConfigDict(populate_by_name=True)

    id: int
    name: str
    k8s_cluster: VmK8sCluster = Field(alias="k8s-cluster")


class VmRepository(ABC):
    @abstractmethod
    async def lookup_by_name(self, node_name: str) -> VmInfo: ...


class HttpVmRepository(VmRepository):
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def lookup_by_name(self, node_name: str) -> VmInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/vms",
                    params={"name": node_name},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"VM lookup for '{node_name}' timed out after {self._timeout}s.",
                detail={"node_name": node_name},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"VM lookup for '{node_name}' failed: {exc}",
                detail={"node_name": node_name},
            ) from exc

        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"VM API returned {resp.status_code} for '{node_name}'.",
                detail={"node_name": node_name, "status_code": resp.status_code},
            )

        payload = resp.json()
        results = payload.get("results", [])
        if len(results) == 0:
            raise NotFoundException(
                f"VM '{node_name}' not found.",
                detail={"node_name": node_name},
            )
        if len(results) > 1:
            raise UpstreamUnavailableException(
                f"VM lookup for '{node_name}' returned {len(results)} results; "
                "expected exactly 1 (unique-name invariant).",
                detail={"node_name": node_name, "result_count": len(results)},
            )

        return VmInfo.model_validate(results[0])
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
APP_ENV=test uv run pytest tests/unit/test_vm_repository.py -v
```

Expected: all 6 pass.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/vm_repository.py tests/unit/test_vm_repository.py
git commit -m "feat(repo): add VmRepository with unique-name invariant"
```

---

## Task 5: `BastionMappingRepository` ABC + HTTP implementation

**Files:**

- Create: `app/repositories/bastion_mapping_repository.py`
- Create: `tests/unit/test_bastion_mapping_repository.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_bastion_mapping_repository.py`:

```python
import httpx
import pytest

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.bastion_mapping_repository import (
    BastionMapping,
    HttpBastionMappingRepository,
)


def _repo(handler) -> HttpBastionMappingRepository:
    transport = httpx.MockTransport(handler)
    return HttpBastionMappingRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


async def test_list_success_returns_mappings_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bastion-cluster-mappings"
        assert dict(request.url.params) == {"type": "type1"}
        assert request.headers.get("authorization") == "Bearer t"
        return httpx.Response(
            200,
            json={
                "count": 2,
                "results": [
                    {
                        "pattern": ["type1-cluster-(c1|c2)", "type1-cluster.*"],
                        "runner": "r1",
                        "bastion": "b1",
                        "bastion_ip": "10.0.0.1",
                    },
                    {
                        "pattern": ["type1-kind"],
                        "runner": "r2",
                        "bastion": "b2",
                        "bastion_ip": "10.0.0.2",
                    },
                ],
            },
        )

    repo = _repo(handler)
    result = await repo.list_mappings("type1")
    assert result == [
        BastionMapping(
            pattern=["type1-cluster-(c1|c2)", "type1-cluster.*"],
            runner="r1",
            bastion="b1",
            bastion_ip="10.0.0.1",
        ),
        BastionMapping(
            pattern=["type1-kind"],
            runner="r2",
            bastion="b2",
            bastion_ip="10.0.0.2",
        ),
    ]


async def test_list_empty_results_raises_not_found():
    repo = _repo(lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    with pytest.raises(NotFoundException):
        await repo.list_mappings("unknown")


async def test_list_500_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.list_mappings("type1")


async def test_list_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
APP_ENV=test uv run pytest tests/unit/test_bastion_mapping_repository.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement the repository**

Create `app/repositories/bastion_mapping_repository.py`:

```python
"""Bastion-Cluster mapping API repository.

GET {base_url}/api/v1/bastion-cluster-mappings?type={type_name}

Contract:
  - 200 + non-empty results → list[BastionMapping] (priority preserved)
  - 200 + empty results     → NotFoundException (unknown type)
  - timeout                 → UpstreamTimeoutException
  - other 4xx / 5xx / net   → UpstreamUnavailableException
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

import httpx
from pydantic import BaseModel

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


class BastionMapping(BaseModel):
    pattern: List[str]
    runner: str
    bastion: str
    bastion_ip: str


class BastionMappingRepository(ABC):
    @abstractmethod
    async def list_mappings(self, type_name: str) -> List[BastionMapping]: ...


class HttpBastionMappingRepository(BastionMappingRepository):
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/bastion-cluster-mappings",
                    params={"type": type_name},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"Bastion mapping lookup for type '{type_name}' "
                f"timed out after {self._timeout}s.",
                detail={"type": type_name},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"Bastion mapping lookup for type '{type_name}' failed: {exc}",
                detail={"type": type_name},
            ) from exc

        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"Bastion mapping API returned {resp.status_code} for type '{type_name}'.",
                detail={"type": type_name, "status_code": resp.status_code},
            )

        results = resp.json().get("results", [])
        if not results:
            raise NotFoundException(
                f"No bastion mappings found for type '{type_name}'.",
                detail={"type": type_name},
            )
        return [BastionMapping.model_validate(item) for item in results]
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
APP_ENV=test uv run pytest tests/unit/test_bastion_mapping_repository.py -v
```

Expected: all 5 pass.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/bastion_mapping_repository.py tests/unit/test_bastion_mapping_repository.py
git commit -m "feat(repo): add BastionMappingRepository"
```

---

## Task 6: In-memory test fixtures for new repos

**Files:**

- Create: `tests/fixtures/cluster.py`

In-memory implementations of `VmRepository` and `BastionMappingRepository` for resolver / integration tests. Mirrors `tests/fixtures/inventory.py`.

- [ ] **Step 1: Implement fixtures**

Create `tests/fixtures/cluster.py`:

```python
"""In-memory VmRepository + BastionMappingRepository for tests."""

from typing import Dict, List

from app.core.exceptions import NotFoundException
from app.repositories.bastion_mapping_repository import (
    BastionMapping,
    BastionMappingRepository,
)
from app.repositories.vm_repository import VmInfo, VmRepository


class InMemoryVmRepository(VmRepository):
    def __init__(self, records: Dict[str, VmInfo]):
        self._records = records

    async def lookup_by_name(self, node_name: str) -> VmInfo:
        info = self._records.get(node_name)
        if info is None:
            raise NotFoundException(
                f"VM '{node_name}' not found.",
                detail={"node_name": node_name},
            )
        return info


class InMemoryBastionMappingRepository(BastionMappingRepository):
    def __init__(self, mappings_by_type: Dict[str, List[BastionMapping]]):
        self._mappings_by_type = mappings_by_type

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        mappings = self._mappings_by_type.get(type_name)
        if not mappings:
            raise NotFoundException(
                f"No bastion mappings found for type '{type_name}'.",
                detail={"type": type_name},
            )
        return mappings
```

- [ ] **Step 2: Smoke-check import**

```bash
APP_ENV=test uv run python -c "from tests.fixtures.cluster import InMemoryVmRepository, InMemoryBastionMappingRepository; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/cluster.py
git commit -m "test: in-memory VmRepository + BastionMappingRepository fixtures"
```

---

## Task 7: Replace `BastionHostResolver` with `ClusterBastionHostResolver`

**Files:**

- Modify: `app/repositories/host_resolver.py`
- Modify: `tests/unit/test_host_resolver.py`
- Create: `tests/unit/test_cluster_bastion_resolver.py`

This is the load-bearing logic change. The old `BastionHostResolver` is deleted; the factory grows keyword-only args for the new dependencies. Existing tests in `test_host_resolver.py` that reference `BastionHostResolver` must be updated in the same task to keep the suite green.

- [ ] **Step 1: Write failing tests for the new resolver**

Create `tests/unit/test_cluster_bastion_resolver.py`:

```python
import pytest

from app.core.exceptions import NotFoundException
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.host_resolver import ClusterBastionHostResolver
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryVmRepository,
)


def _vm_repo(cluster_name: str) -> InMemoryVmRepository:
    return InMemoryVmRepository(
        {
            "node1": VmInfo(
                id=1,
                name="node1",
                k8s_cluster=VmK8sCluster(id=1, name=cluster_name),
            )
        }
    )


def _mapping_repo(mappings_by_type):
    return InMemoryBastionMappingRepository(mappings_by_type)


async def test_first_pattern_in_first_entry_wins():
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                pattern=["type1-kind"],
                runner="r2",
                bastion="b2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c1"), _mapping_repo(mappings), "type1"
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.0.0.1"
    assert resolved.source_input == "node1"
    assert resolved.metadata == {
        "node_name": "node1",
        "cluster_name": "type1-cluster-c1",
        "bastion_hostname": "b1",
        "bastion_type": "type1",
        "matched_pattern": "type1-cluster-(c1|c2|c3)",
    }


async def test_second_entry_priority_when_first_doesnt_match():
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                pattern=["type1-kind"],
                runner="r2",
                bastion="b2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-kind"), _mapping_repo(mappings), "type1"
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.0.0.2"
    assert resolved.metadata["matched_pattern"] == "type1-kind"


async def test_no_pattern_matches_raises_not_found():
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c99"), _mapping_repo(mappings), "type1"
    )
    with pytest.raises(NotFoundException) as exc_info:
        await resolver.resolve("node1")
    detail = exc_info.value.detail
    assert detail["node_name"] == "node1"
    assert detail["cluster_name"] == "type1-cluster-c99"
    assert detail["bastion_type"] == "type1"


async def test_vm_not_found_propagates():
    mappings = {"type1": [BastionMapping(pattern=[".*"], runner="r", bastion="b", bastion_ip="1.1.1.1")]}
    resolver = ClusterBastionHostResolver(
        InMemoryVmRepository({}),  # empty
        _mapping_repo(mappings),
        "type1",
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing-node")


async def test_fullmatch_boundary_dotstar():
    """type1-cluster.* matches the whole string only when re.fullmatch is used."""
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster.*"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    # Matches: "type1-cluster-c1", "type1-cluster", "type1-clusterX"
    for cluster in ["type1-cluster-c1", "type1-cluster", "type1-clusterX"]:
        resolver = ClusterBastionHostResolver(
            _vm_repo(cluster), _mapping_repo(mappings), "type1"
        )
        resolved = await resolver.resolve("node1")
        assert resolved.ip == "10.0.0.1", f"should match {cluster!r}"


async def test_fullmatch_boundary_strict_alternation():
    """'type1-cluster-(c1|c2|c3)' under fullmatch matches '...c1' but not '...c99'."""
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    # Matches c1
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c1"), _mapping_repo(mappings), "type1"
    )
    assert (await resolver.resolve("node1")).ip == "10.0.0.1"
    # Does NOT match c99 → no-match → NotFoundException
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c99"), _mapping_repo(mappings), "type1"
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("node1")
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
APP_ENV=test uv run pytest tests/unit/test_cluster_bastion_resolver.py -v
```

Expected: ImportError on `ClusterBastionHostResolver`.

- [ ] **Step 3: Rewrite the resolver**

Replace the contents of `app/repositories/host_resolver.py`:

```python
"""Host resolver strategy: chooses the SSH target IP based on host_type.

Adding a new host type:
  1. Add a value to HostType in app/domain/command.py.
  2. Add a HostResolver subclass here.
  3. Add a branch to create_host_resolver().
CommandService does not need to change.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Dict, Optional

from pydantic import BaseModel, Field

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.bastion_mapping_repository import BastionMappingRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.vm_repository import VmRepository


class ResolvedHost(BaseModel):
    ip: str
    source_input: str
    metadata: Dict[str, str] = Field(default_factory=dict)


class HostResolver(ABC):
    @abstractmethod
    async def resolve(self, raw_host: str) -> ResolvedHost: ...


class IpHostResolver(HostResolver):
    async def resolve(self, raw_host: str) -> ResolvedHost:
        return ResolvedHost(ip=raw_host, source_input=raw_host)


class HostnameHostResolver(HostResolver):
    def __init__(self, inventory: InventoryRepository) -> None:
        self._inventory = inventory

    async def resolve(self, raw_host: str) -> ResolvedHost:
        info = await self._inventory.lookup(raw_host)
        return ResolvedHost(
            ip=info.ip,
            source_input=raw_host,
            metadata={"hostname": info.hostname},
        )


class ClusterBastionHostResolver(HostResolver):
    """Resolve node_name → cluster_name → bastion_ip via two API calls."""

    def __init__(
        self,
        vm_repo: VmRepository,
        mapping_repo: BastionMappingRepository,
        bastion_type: str,
    ) -> None:
        self._vm_repo = vm_repo
        self._mapping_repo = mapping_repo
        self._bastion_type = bastion_type

    async def resolve(self, raw_host: str) -> ResolvedHost:
        vm = await self._vm_repo.lookup_by_name(raw_host)
        cluster_name = vm.k8s_cluster.name

        mappings = await self._mapping_repo.list_mappings(self._bastion_type)

        for mapping in mappings:
            for pattern in mapping.pattern:
                if re.fullmatch(pattern, cluster_name):
                    return ResolvedHost(
                        ip=mapping.bastion_ip,
                        source_input=raw_host,
                        metadata={
                            "node_name": raw_host,
                            "cluster_name": cluster_name,
                            "bastion_hostname": mapping.bastion,
                            "bastion_type": self._bastion_type,
                            "matched_pattern": pattern,
                        },
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{self._bastion_type}'.",
            detail={
                "node_name": raw_host,
                "cluster_name": cluster_name,
                "bastion_type": self._bastion_type,
            },
        )


def create_host_resolver(
    host_type: HostType,
    *,
    inventory: Optional[InventoryRepository] = None,
    vm_repo: Optional[VmRepository] = None,
    mapping_repo: Optional[BastionMappingRepository] = None,
    bastion_type: Optional[str] = None,
) -> HostResolver:
    if host_type == HostType.IP:
        return IpHostResolver()
    if host_type == HostType.HOSTNAME:
        if inventory is None:
            raise ValueError("HOSTNAME resolver requires inventory")
        return HostnameHostResolver(inventory)
    if host_type == HostType.BASTION:
        if vm_repo is None or mapping_repo is None or bastion_type is None:
            raise ValueError(
                "BASTION resolver requires vm_repo, mapping_repo, bastion_type"
            )
        return ClusterBastionHostResolver(vm_repo, mapping_repo, bastion_type)
    raise ValueError(f"Unsupported host_type: {host_type}")
```

- [ ] **Step 4: Update existing host resolver tests**

Replace the contents of `tests/unit/test_host_resolver.py`:

```python
import pytest

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.host_resolver import (
    ClusterBastionHostResolver,
    HostnameHostResolver,
    IpHostResolver,
    ResolvedHost,
    create_host_resolver,
)
from app.repositories.inventory_repository import (
    InventoryBastion,
    InventoryHostInfo,
)
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryVmRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _inventory():
    return InMemoryInventoryRepository({
        "node-a01": InventoryHostInfo(
            hostname="node-a01", ip="10.0.1.10",
            bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
        ),
    })


def _vm_repo():
    return InMemoryVmRepository({
        "node1": VmInfo(
            id=1, name="node1",
            k8s_cluster=VmK8sCluster(id=1, name="type1-cluster-c1"),
        ),
    })


def _mapping_repo():
    return InMemoryBastionMappingRepository({
        "type1": [
            BastionMapping(
                pattern=["type1-cluster.*"],
                runner="r", bastion="b", bastion_ip="10.0.0.1",
            )
        ]
    })


async def test_ip_resolver_returns_input_unchanged():
    resolver = IpHostResolver()
    resolved = await resolver.resolve("10.0.0.1")
    assert resolved == ResolvedHost(ip="10.0.0.1", source_input="10.0.0.1", metadata={})


async def test_hostname_resolver_returns_host_ip():
    resolver = HostnameHostResolver(_inventory())
    resolved = await resolver.resolve("node-a01")
    assert resolved.ip == "10.0.1.10"
    assert resolved.source_input == "node-a01"
    assert resolved.metadata == {"hostname": "node-a01"}


async def test_hostname_resolver_propagates_not_found():
    resolver = HostnameHostResolver(_inventory())
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing")


def test_factory_returns_correct_resolver_class():
    assert isinstance(
        create_host_resolver(HostType.IP), IpHostResolver
    )
    assert isinstance(
        create_host_resolver(HostType.HOSTNAME, inventory=_inventory()),
        HostnameHostResolver,
    )
    assert isinstance(
        create_host_resolver(
            HostType.BASTION,
            vm_repo=_vm_repo(),
            mapping_repo=_mapping_repo(),
            bastion_type="type1",
        ),
        ClusterBastionHostResolver,
    )


def test_factory_bastion_missing_deps_raises():
    with pytest.raises(ValueError):
        create_host_resolver(HostType.BASTION)
```

- [ ] **Step 5: Run resolver tests**

```bash
APP_ENV=test uv run pytest tests/unit/test_host_resolver.py tests/unit/test_cluster_bastion_resolver.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/repositories/host_resolver.py tests/unit/test_host_resolver.py tests/unit/test_cluster_bastion_resolver.py
git commit -m "feat(resolver): replace BastionHostResolver with ClusterBastionHostResolver"
```

---

## Task 8: `CommandOption.bastion_type` + service wiring

**Files:**

- Modify: `app/domain/command.py`
- Modify: `app/services/command_service.py`
- Modify: `tests/unit/test_command_service.py` (extend)

The schema gains a new optional field. `CommandService.__init__` takes the two new repos; `_prepare_execution` computes `bastion_type` and passes everything to the factory. Old call sites that constructed `CommandService(repo, inventory)` are not touched by this task — they get fixed in Task 9 (DI factory).

- [ ] **Step 1: Add field to `CommandOption`**

Edit `app/domain/command.py`. Replace the `CommandOption` class with:

```python
class CommandOption(BaseModel):
    timeout_seconds: int = 30
    bastion_type: Optional[str] = None  # None → fall back to settings.BASTION_DEFAULT_TYPE
```

(The `Optional` import is already present at the top of the file.)

- [ ] **Step 2: Update `CommandService.__init__` and `_prepare_execution`**

Edit `app/services/command_service.py`.

First, add the imports near the existing repository imports:

```python
from app.repositories.bastion_mapping_repository import BastionMappingRepository
from app.repositories.vm_repository import VmRepository
```

Replace `CommandService.__init__`:

```python
    def __init__(
        self,
        repo: CommandStateRepository,
        inventory: Optional[InventoryRepository],
        vm_repo: Optional[VmRepository] = None,
        mapping_repo: Optional[BastionMappingRepository] = None,
    ):
        self.repo = repo
        self.inventory = inventory
        self.vm_repo = vm_repo
        self.mapping_repo = mapping_repo
```

Inside `_prepare_execution`, find the existing line:

```python
        resolver = create_host_resolver(req.host_type, self.inventory)
```

Replace it with:

```python
        bastion_type = (
            req.option.bastion_type
            if req.option and req.option.bastion_type
            else settings.BASTION_DEFAULT_TYPE
        )
        resolver = create_host_resolver(
            req.host_type,
            inventory=self.inventory,
            vm_repo=self.vm_repo,
            mapping_repo=self.mapping_repo,
            bastion_type=bastion_type,
        )
```

- [ ] **Step 3: Add unit tests for the bastion_type wiring**

Append to `tests/unit/test_command_service.py`:

```python
# ── bastion_type wiring ────────────────────────────────────────────────────

from unittest.mock import AsyncMock, MagicMock
import pytest

from app.core.config import get_settings
from app.domain.command import (
    CommandExecutionRequest, CommandOption, HostType,
)
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from app.services.command_service import CommandService
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository, InMemoryVmRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _service_for_bastion(vm_repo, mapping_repo):
    """Build a CommandService with minimum deps for _prepare_execution to run."""
    state_repo = MagicMock()
    state_repo.save = AsyncMock()
    return CommandService(
        repo=state_repo,
        inventory=InMemoryInventoryRepository({}),
        vm_repo=vm_repo,
        mapping_repo=mapping_repo,
    )


def _vm(cluster: str) -> InMemoryVmRepository:
    return InMemoryVmRepository({
        "n1": VmInfo(id=1, name="n1", k8s_cluster=VmK8sCluster(id=1, name=cluster))
    })


def _mapping(type_name: str, ip: str) -> InMemoryBastionMappingRepository:
    return InMemoryBastionMappingRepository({
        type_name: [BastionMapping(
            pattern=[".*"], runner="r", bastion="b", bastion_ip=ip,
        )]
    })


async def test_bastion_type_explicit_in_option_is_used():
    """When option.bastion_type='type2' is set, mapping_repo is called with 'type2'."""
    vm = _vm("type2-cluster-x")
    # Only 'type2' has a mapping; if the resolver asked for any other type it would 404.
    mapping = _mapping("type2", "10.10.10.10")
    svc = _service_for_bastion(vm, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        option=CommandOption(timeout_seconds=30, bastion_type="type2"),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.10.10.10"
    assert ctx.resolved_host.metadata["bastion_type"] == "type2"


async def test_bastion_type_defaults_to_settings_when_option_none():
    """When option.bastion_type is None, mapping_repo is called with BASTION_DEFAULT_TYPE."""
    default_type = get_settings().BASTION_DEFAULT_TYPE
    vm = _vm("anything")
    mapping = _mapping(default_type, "10.20.30.40")
    svc = _service_for_bastion(vm, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        option=CommandOption(timeout_seconds=30),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.20.30.40"
    assert ctx.resolved_host.metadata["bastion_type"] == default_type
```

This test relies on the `tests/fixtures/users.json` `test_admin` entry having the `command_api` scope and on the test SSH-default + whitelist fixtures being present. Both are already configured in `tests/conftest.py` (the SSH key fixture is auto-generated) and in `.env.test` (`COMMAND_CONFIG_DIR=tests/fixtures`).

- [ ] **Step 4: Run tests**

```bash
APP_ENV=test uv run pytest tests/unit/test_command_service.py -v
```

Expected: all existing tests still pass plus the two new ones.

- [ ] **Step 5: Commit**

```bash
git add app/domain/command.py app/services/command_service.py tests/unit/test_command_service.py
git commit -m "feat(command): wire bastion_type through CommandService"
```

---

## Task 9: Dependency Injection wiring

**Files:**

- Modify: `app/core/dependencies.py`

Add the two new DI factories and update `get_command_service` to inject them.

- [ ] **Step 1: Update dependencies.py**

Edit `app/core/dependencies.py`.

Add new imports below the existing `from app.repositories.inventory_repository import ...` block:

```python
from app.repositories.vm_repository import (
    HttpVmRepository,
    VmRepository,
)
from app.repositories.bastion_mapping_repository import (
    HttpBastionMappingRepository,
    BastionMappingRepository,
)
```

Add the two new factory functions after `get_inventory_repository`:

```python
async def get_vm_repository() -> VmRepository:
    s = get_settings()
    return HttpVmRepository(
        base_url=s.CLUSTER_API_URL,
        token=s.CLUSTER_API_TOKEN,
        timeout_seconds=s.CLUSTER_API_TIMEOUT_SECONDS,
    )


async def get_bastion_mapping_repository() -> BastionMappingRepository:
    s = get_settings()
    return HttpBastionMappingRepository(
        base_url=s.CLUSTER_API_URL,
        token=s.CLUSTER_API_TOKEN,
        timeout_seconds=s.CLUSTER_API_TIMEOUT_SECONDS,
    )
```

Replace `get_command_service`:

```python
async def get_command_service(
    repo: CommandStateRepository = Depends(get_command_state_repository),
    inventory: InventoryRepository = Depends(get_inventory_repository),
    vm_repo: VmRepository = Depends(get_vm_repository),
    mapping_repo: BastionMappingRepository = Depends(get_bastion_mapping_repository),
) -> CommandService:
    return CommandService(repo, inventory, vm_repo, mapping_repo)
```

- [ ] **Step 2: Smoke-check app starts**

```bash
APP_ENV=test uv run python -c "from app.main import create_app; create_app(); print('ok')"
```

Expected: `ok` with no traceback.

- [ ] **Step 3: Commit**

```bash
git add app/core/dependencies.py
git commit -m "feat(di): wire VmRepository and BastionMappingRepository into CommandService"
```

---

## Task 10: Update integration tests for the new bastion path

**Files:**

- Modify: `tests/integration/test_command_host_type.py`
- Create: `tests/integration/test_command_bastion_cluster_flow.py`

`test_command_host_type.py::test_host_type_bastion_connects_to_bastion_ip` currently expects `10.0.0.5` from the inventory's `bastion.ip`. That path is gone. Replace its bastion test with one that overrides the new DI factories. Then add a dedicated file for richer scenarios (priority, default type, type override, not-found).

- [ ] **Step 1: Rewrite the bastion test in test_command_host_type.py**

Edit `tests/integration/test_command_host_type.py`.

First, add the new imports near the top:

```python
from app.core.dependencies import (
    get_bastion_mapping_repository,
    get_command_state_repository,
    get_inventory_repository,
    get_vm_repository,
)
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryVmRepository,
)
```

Then add a new fixture below the existing `client_with_inventory` fixture:

```python
@pytest.fixture
def client_with_bastion(inventory):
    """TestClient with inventory, vm, and bastion-mapping repos all overridden."""
    app = create_app()
    state_repo = _InMemoryCommandStateRepo()
    vm_repo = InMemoryVmRepository({
        "node1": VmInfo(
            id=1, name="node1",
            k8s_cluster=VmK8sCluster(id=1, name="type1-cluster-c1"),
        ),
    })
    mapping_repo = InMemoryBastionMappingRepository({
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="r1", bastion="bastion-type1",
                bastion_ip="10.99.99.1",
            )
        ]
    })
    app.dependency_overrides[get_inventory_repository] = lambda: inventory
    app.dependency_overrides[get_command_state_repository] = lambda: state_repo
    app.dependency_overrides[get_vm_repository] = lambda: vm_repo
    app.dependency_overrides[get_bastion_mapping_repository] = lambda: mapping_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

Replace the existing `test_host_type_bastion_connects_to_bastion_ip` function (and delete the old body that asserted `10.0.0.5`) with:

```python
def test_host_type_bastion_connects_to_mapped_bastion_ip(client_with_bastion):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_bastion)
        resp = client_with_bastion.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "node1",
                "host_type": "bastion",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.99.99.1"
```

- [ ] **Step 2: Create the dedicated cluster-flow file**

Create `tests/integration/test_command_bastion_cluster_flow.py`:

```python
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
```

- [ ] **Step 3: Run integration tests**

```bash
APP_ENV=test uv run pytest tests/integration/test_command_host_type.py tests/integration/test_command_bastion_cluster_flow.py -v
```

Expected: all pass.

- [ ] **Step 4: Run the full suite to catch any regressions**

```bash
APP_ENV=test uv run pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_command_host_type.py tests/integration/test_command_bastion_cluster_flow.py
git commit -m "test(integration): cover bastion cluster mapping resolution"
```

---

## Task 11: Documentation

**Files:**

- Modify: `ssh-command.md`

Replace any `host_type=bastion` section that references the inventory's `bastion.ip` with the new two-step resolution.

- [ ] **Step 1: Locate the bastion section**

```bash
grep -n -i "bastion" ssh-command.md | head -40
```

Identify the section that describes `host_type=bastion` resolution.

- [ ] **Step 2: Update the bastion section**

In `ssh-command.md`, replace the existing `host_type=bastion` description with:

```markdown
### `host_type=bastion`

Resolution chain:

1. `GET {CLUSTER_API_URL}/api/v1/vms?name={raw_host}` — look up the node by name; expect exactly one result. Empty results → `404 NOT_FOUND`. More than one result → `502 UPSTREAM_UNAVAILABLE` (unique-name invariant).
2. Read `vm.k8s_cluster.name` as `cluster_name`.
3. `GET {CLUSTER_API_URL}/api/v1/bastion-cluster-mappings?type={bastion_type}` — list all mappings for the selected type. Empty → `404 NOT_FOUND`.
4. Iterate `results` top-to-bottom; within each entry iterate `pattern` top-to-bottom. The first `re.fullmatch(pattern, cluster_name)` hit selects the entry's `bastion_ip`. No match → `404 NOT_FOUND`.

`bastion_type` is taken from `option.bastion_type` in the request body. If omitted (or `null`), the service falls back to the `BASTION_DEFAULT_TYPE` environment variable.

Old clients that do not send `option.bastion_type` continue to work — they implicitly use the default type. The HTTP route and query parameters of `POST /api/v1/command/execution` are unchanged.
```

- [ ] **Step 3: Commit**

```bash
git add ssh-command.md
git commit -m "docs: describe the new host_type=bastion resolution chain"
```

---

## Task 12: Final verification

**Files:** (none modified)

Run the full test suite and a quick manual smoke against the fake-API to confirm everything is wired.

- [ ] **Step 1: Run the full test suite**

```bash
APP_ENV=test uv run pytest tests/ -v
```

Expected: every test passes.

- [ ] **Step 2: Start the fake-API and verify the new endpoints respond**

In one terminal:

```bash
make inventory-api
```

In another terminal, with the fake-API running:

```bash
curl -s -H "Authorization: Bearer fake-cluster-token" \
  "http://localhost:9001/api/v1/vms?name=node1" | python -m json.tool

curl -s -H "Authorization: Bearer fake-cluster-token" \
  "http://localhost:9001/api/v1/bastion-cluster-mappings?type=type1" | python -m json.tool

curl -s -H "Authorization: Bearer fake-cluster-token" \
  "http://localhost:9001/api/v1/vms?name=does-not-exist" | python -m json.tool

curl -s -H "Authorization: Bearer fake-cluster-token" \
  "http://localhost:9001/api/v1/bastion-cluster-mappings?type=does-not-exist" | python -m json.tool
```

Expected: first two return populated payloads; last two return `{"count": 0, ..., "results": []}`.

Stop the fake-API (Ctrl-C).

- [ ] **Step 3: Final commit (only if any files were touched during verification)**

If verification produced no file changes, skip. Otherwise:

```bash
git status
# review what changed and commit appropriately
```

---

## Self-Review

**Spec coverage** (against `2026-06-02-bastion-cluster-mapping-design.md`):

- §2.1 `/api/v1/vms` endpoint → Task 2
- §2.2 `/api/v1/bastion-cluster-mappings` endpoint → Task 2
- §2.3 fixture `*` → `.*` fix → Task 1
- §3.1 `VmRepository` (incl. multi-result invariant) → Task 4
- §3.2 `BastionMappingRepository` → Task 5
- §4.1 delete old `BastionHostResolver` → Task 7
- §4.2 add `ClusterBastionHostResolver` → Task 7
- §4.3 factory signature update → Task 7
- §5.1 `CommandOption.bastion_type` → Task 8
- §5.2 default resolution + factory passthrough → Task 8
- §5.3 kill path unaffected → no task (no change needed; covered implicitly by full suite in Task 12)
- §6 settings + env files → Task 3
- §7 DI factories → Task 9
- §8 error mapping → tests cover in Tasks 4, 5, 7, 10
- §9.1 unit tests (vm, mapping, resolver, command_service) → Tasks 4, 5, 7, 8
- §9.2 integration test → Task 10
- §9.3 fake-api unit tests → Task 2
- §10 backwards compatibility → asserted in Task 10 (default-type test; no body changes for old clients)
- §11 documentation → Task 11
- §12 out-of-scope items → not implemented (correct)

**Placeholder scan:** No TBD/TODO/`# implement later` strings. All code blocks contain full code.

**Type consistency:**

- `VmInfo`, `VmK8sCluster` — defined in Task 4, consumed in Tasks 6/7/8/10.
- `BastionMapping` — defined in Task 5, consumed in Tasks 6/7/8/10.
- `ClusterBastionHostResolver(vm_repo, mapping_repo, bastion_type)` — same positional signature everywhere.
- `create_host_resolver(host_type, *, inventory, vm_repo, mapping_repo, bastion_type)` — same keyword args used in Tasks 7, 8, 9.
- `CommandService(repo, inventory, vm_repo, mapping_repo)` — Tasks 8 and 9 match.
- `lookup_by_name` / `list_mappings` — only methods referenced; consistent across tasks.
- Settings: `CLUSTER_API_URL`, `CLUSTER_API_TOKEN`, `CLUSTER_API_TIMEOUT_SECONDS`, `BASTION_DEFAULT_TYPE` — same names in config, env files, DI factories.

No inconsistencies found.
