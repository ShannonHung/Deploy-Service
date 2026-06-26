# Cluster Host Type & Admin Running-Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `cluster` host type (slash-presence selects bastion_type → existing inventory mappings), a cluster bastion-resolution query endpoint, and an admin-only running-jobs endpoint backed by a Redis scan.

**Architecture:** Three additive features in `deploy-service/`. The cluster resolver reuses the existing `list_mappings(type)` + regex-match path but derives `bastion_type` from whether the cluster_name contains `/` (no node-lookup). A shared `cluster_type_from_name` helper keeps the resolver (execution path) and the inventory service (read-only endpoint) in lockstep. The running-jobs endpoint scans `command:*` Redis keys via `scan_iter` and is gated by a new `admin_api` scope.

**Tech Stack:** FastAPI, Pydantic v2, asyncssh, redis.asyncio, pytest (`asyncio_mode=auto`), uv.

## Global Constraints

- Run all commands from `deploy-service/`. Tests: `APP_ENV=test uv run pytest <path> -v`.
- Layered architecture: router → service → repository interface → impl. Services depend only on ABC repos.
- `get_settings()` is `lru_cache`'d — in tests that change settings, call `get_settings.cache_clear()`.
- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- Routes return `ApiResponse[T]` (`{"data": ..., "request_id": ...}`).
- Scope enforcement: `Depends(get_current_user([...]))`; a valid token missing a scope → `ForbiddenException` (HTTP 403).
- `CLUSTER_SLASH_TYPE_MAP` keys MUST be exactly `"no_slash"` and `"with_slash"`.
- Fixture password for all `tests/fixtures/users.json` accounts is `secret`, bcrypt hash `$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW`.

---

## File Structure

- `app/domain/command.py` — add `HostType.CLUSTER`; add `RunningCommandsResponse`.
- `app/core/config.py` — add `CLUSTER_SLASH_TYPE_MAP`.
- `app/repositories/host_resolver.py` — add `cluster_type_from_name` helper, `ClusterNameResolver`, factory branch + `slash_map` param.
- `app/repositories/inventory_repository.py` — add `ClusterBastionResolution` model.
- `app/repositories/command_state_repository.py` — add `list_states`.
- `app/services/inventory_service.py` — add `slash_map` to `__init__`, add `resolve_cluster_bastion`.
- `app/services/command_service.py` — pass `slash_map` in `_prepare_execution`; add `list_running_commands`.
- `app/core/dependencies.py` — pass `slash_map` to `get_inventory_service`.
- `app/api/v1/inventory.py` — add cluster bastion-resolution route.
- `app/api/v1/command.py` — add `/command/running` route.
- `data/users.json`, `tests/fixtures/users.json` — add `admin_api` scope (+ a `command_api`-only fixture user).
- `.env.test` — set `CLUSTER_SLASH_TYPE_MAP`.

---

## Task 1: `HostType.CLUSTER` + slash→type helper

**Files:**
- Modify: `app/domain/command.py` (the `HostType` enum, ~line 18-21)
- Modify: `app/core/config.py` (after `INVENTORY_IP_LABEL`, ~line 89)
- Modify: `app/repositories/host_resolver.py` (add helper near top, after imports)
- Test: `tests/unit/test_cluster_type_from_name.py` (create)

**Interfaces:**
- Produces:
  - `HostType.CLUSTER = "cluster"`
  - `Settings.CLUSTER_SLASH_TYPE_MAP: Dict[str, str]`
  - `cluster_type_from_name(cluster_name: str, slash_map: Dict[str, str]) -> tuple[str, bool]` in `host_resolver.py` — returns `(bastion_type, has_slash)`; raises `CommandExecutionException` if the required key (`"with_slash"`/`"no_slash"`) is missing from `slash_map`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cluster_type_from_name.py`:

```python
import pytest

from app.core.exceptions import CommandExecutionException
from app.repositories.host_resolver import cluster_type_from_name

SLASH_MAP = {"no_slash": "type1", "with_slash": "type2"}


def test_no_slash_selects_no_slash_type():
    assert cluster_type_from_name("taiwan-taipei-my-cluster", SLASH_MAP) == ("type1", False)


def test_slash_selects_with_slash_type():
    assert cluster_type_from_name("taiwan-taipei/my-cluster", SLASH_MAP) == ("type2", True)


def test_missing_key_raises():
    with pytest.raises(CommandExecutionException) as exc:
        cluster_type_from_name("a/b", {"no_slash": "type1"})
    assert "with_slash" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_cluster_type_from_name.py -v`
Expected: FAIL — `ImportError: cannot import name 'cluster_type_from_name'`.

- [ ] **Step 3: Add `HostType.CLUSTER`**

In `app/domain/command.py`, extend the enum:

```python
class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"
    CLUSTER = "cluster"
```

- [ ] **Step 4: Add config setting**

In `app/core/config.py`, after the `INVENTORY_IP_LABEL` line (~89):

```python
    # Maps slash-presence of a cluster_name to a bastion_type. Keys MUST be
    # "no_slash" and "with_slash". Example:
    #   CLUSTER_SLASH_TYPE_MAP='{"no_slash": "type1", "with_slash": "type2"}'
    CLUSTER_SLASH_TYPE_MAP: Dict[str, str] = {}
```

- [ ] **Step 5: Add the helper**

In `app/repositories/host_resolver.py`, after the existing imports (the module already imports `CommandExecutionException` and `Dict`):

```python
def cluster_type_from_name(
    cluster_name: str, slash_map: Dict[str, str]
) -> tuple[str, bool]:
    """Derive (bastion_type, has_slash) from a cluster_name.

    Slash-presence selects the key in slash_map: "with_slash" when the name
    contains '/', else "no_slash". Raises CommandExecutionException naming the
    missing key if slash_map lacks it (operator misconfig).
    """
    has_slash = "/" in cluster_name
    key = "with_slash" if has_slash else "no_slash"
    bastion_type = slash_map.get(key)
    if bastion_type is None:
        raise CommandExecutionException(
            f"CLUSTER_SLASH_TYPE_MAP is missing key '{key}'. "
            f"Current map: {slash_map}. Add both 'no_slash' and 'with_slash'.",
            detail={"missing_key": key, "slash_map": slash_map},
        )
    return bastion_type, has_slash
```

- [ ] **Step 6: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_cluster_type_from_name.py -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add app/domain/command.py app/core/config.py app/repositories/host_resolver.py tests/unit/test_cluster_type_from_name.py
git commit -m "feat(host): cluster host_type enum + slash-to-type helper"
```

---

## Task 2: `ClusterNameResolver` + factory wiring

**Files:**
- Modify: `app/repositories/host_resolver.py` (add resolver class; extend `create_host_resolver`)
- Test: `tests/unit/test_cluster_name_resolver.py` (create)

**Interfaces:**
- Consumes: `cluster_type_from_name` (Task 1); `InventoryRepository.list_mappings(type) -> List[BastionMapping]`; `ResolvedHost`.
- Produces:
  - `ClusterNameResolver(inventory_repo, slash_map)` with `async def resolve(self, raw_host: str) -> ResolvedHost` (raw_host IS the cluster_name).
  - `create_host_resolver(..., slash_map: Optional[Dict[str, str]] = None)` returns `ClusterNameResolver` for `HostType.CLUSTER`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cluster_name_resolver.py`:

```python
import pytest

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.domain.command import HostType
from app.repositories.host_resolver import (
    ClusterNameResolver,
    create_host_resolver,
)
from app.repositories.inventory_repository import BastionMapping
from tests.fixtures.cluster import InMemoryInventoryRepository

SLASH_MAP = {"no_slash": "type1", "with_slash": "type2"}


def _repo():
    return InMemoryInventoryRepository(
        mappings={
            "type1": [BastionMapping(patterns=["taiwan-.*"], runner="r1", bastion="b1", bastion_ip="10.1.0.1")],
            "type2": [BastionMapping(patterns=["taiwan-taipei/.*"], runner="r2", bastion="b2", bastion_ip="10.2.0.2")],
        }
    )


async def test_no_slash_resolves_via_type1():
    r = ClusterNameResolver(_repo(), SLASH_MAP)
    resolved = await r.resolve("taiwan-taipei-my-cluster")
    assert resolved.ip == "10.1.0.1"
    assert resolved.metadata["bastion_type"] == "type1"
    assert resolved.metadata["has_slash"] == "False"


async def test_slash_resolves_via_type2():
    r = ClusterNameResolver(_repo(), SLASH_MAP)
    resolved = await r.resolve("taiwan-taipei/my-cluster")
    assert resolved.ip == "10.2.0.2"
    assert resolved.metadata["bastion_type"] == "type2"


async def test_no_pattern_match_raises_not_found():
    repo = InMemoryInventoryRepository(
        mappings={"type1": [BastionMapping(patterns=["nope-.*"], runner="r", bastion="b", bastion_ip="9.9.9.9")]}
    )
    r = ClusterNameResolver(repo, SLASH_MAP)
    with pytest.raises(NotFoundException):
        await r.resolve("taiwan-taipei-my-cluster")


def test_factory_returns_cluster_resolver():
    r = create_host_resolver(HostType.CLUSTER, inventory_repo=_repo(), slash_map=SLASH_MAP)
    assert isinstance(r, ClusterNameResolver)


def test_factory_cluster_requires_slash_map():
    with pytest.raises(ValueError):
        create_host_resolver(HostType.CLUSTER, inventory_repo=_repo())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_cluster_name_resolver.py -v`
Expected: FAIL — `ImportError: cannot import name 'ClusterNameResolver'`.

- [ ] **Step 3: Add the resolver class**

In `app/repositories/host_resolver.py`, add after `ClusterBastionHostResolver`:

```python
class ClusterNameResolver(HostResolver):
    """Resolve a cluster_name directly to a bastion IP.

    Slash-presence in the cluster_name selects bastion_type via slash_map
    (see cluster_type_from_name); the cluster_name is then regex-matched
    against the inventory mappings for that type. No node-lookup is performed.
    """

    def __init__(
        self,
        inventory_repo: InventoryRepository,
        slash_map: Dict[str, str],
    ) -> None:
        self._inventory_repo = inventory_repo
        self._slash_map = slash_map

    async def resolve(self, raw_host: str) -> ResolvedHost:
        cluster_name = raw_host
        bastion_type, has_slash = cluster_type_from_name(cluster_name, self._slash_map)
        mappings = await self._inventory_repo.list_mappings(bastion_type)

        for mapping in mappings:
            for pattern in mapping.patterns:
                try:
                    matched = re.fullmatch(pattern, cluster_name)
                except re.error:
                    _logger.warning(
                        "Skipping invalid regex pattern %r in bastion mapping "
                        "(type=%s) — fix the mapping API data",
                        pattern, bastion_type,
                    )
                    continue
                if matched:
                    return ResolvedHost(
                        ip=mapping.bastion_ip,
                        source_input=cluster_name,
                        metadata={
                            "cluster_name": cluster_name,
                            "bastion_type": bastion_type,
                            "has_slash": str(has_slash),
                            "bastion_hostname": mapping.bastion,
                            "matched_pattern": pattern,
                        },
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={"cluster_name": cluster_name, "bastion_type": bastion_type},
        )
```

- [ ] **Step 4: Extend the factory**

In `app/repositories/host_resolver.py`, change `create_host_resolver`'s signature to add `slash_map`, and add the CLUSTER branch before the final `raise`:

```python
def create_host_resolver(
    host_type: HostType,
    *,
    inventory_repo: Optional[InventoryRepository] = None,
    node_type_map: Optional[Dict[str, str]] = None,
    bastion_type: Optional[str] = None,
    ip_label: Optional[str] = None,
    slash_map: Optional[Dict[str, str]] = None,
) -> HostResolver:
```

Add this branch (after the `HostType.BASTION` branch, before the final `raise ValueError`):

```python
    if host_type == HostType.CLUSTER:
        if inventory_repo is None or slash_map is None:
            raise ValueError(
                "CLUSTER resolver requires inventory_repo and slash_map"
            )
        return ClusterNameResolver(inventory_repo, slash_map)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_cluster_name_resolver.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add app/repositories/host_resolver.py tests/unit/test_cluster_name_resolver.py
git commit -m "feat(host): ClusterNameResolver + factory wiring for cluster host_type"
```

---

## Task 3: Wire `cluster` into command execution

**Files:**
- Modify: `app/services/command_service.py` (`_prepare_execution`, the `create_host_resolver(...)` call ~line 425-431)
- Test: `tests/unit/test_command_service.py` (extend) OR `tests/unit/test_command_cluster_prepare.py` (create)

**Interfaces:**
- Consumes: `create_host_resolver(..., slash_map=...)` (Task 2); `settings.CLUSTER_SLASH_TYPE_MAP`.
- Produces: `_prepare_execution` resolves `host_type=cluster` through `ClusterNameResolver`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_cluster_prepare.py`:

```python
import pytest

from app.core.config import get_settings
from app.domain.command import CommandExecutionRequest, HostType
from app.repositories.inventory_repository import BastionMapping
from app.services.command_service import CommandService
from tests.fixtures.cluster import InMemoryInventoryRepository


class _FakeRepo:
    async def get(self, *a, **k): ...


@pytest.fixture(autouse=True)
def _slash_map(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "CLUSTER_SLASH_TYPE_MAP", {"no_slash": "type1", "with_slash": "type2"})
    # command_service caches `settings` at import; patch its module-level copy too.
    import app.services.command_service as cs
    monkeypatch.setattr(cs.settings, "CLUSTER_SLASH_TYPE_MAP", {"no_slash": "type1", "with_slash": "type2"}, raising=False)
    yield


async def test_prepare_execution_resolves_cluster_host(tmp_path, monkeypatch):
    # Whitelist allowing the resolved bastion IP and a trivial command.
    import json, os
    cfg = {
        "name": "admin",
        "allow_hosts": ["10\\.1\\.0\\.1"],
        "deny_hosts": [],
        "allow_commands": [{"command_name": "noop", "pipeline": [{"command": ["true"]}], "arguments": []}],
    }
    (tmp_path / "allow-commands-admin.json").write_text(json.dumps(cfg))
    (tmp_path / "SSH-default.json").write_text(json.dumps({"auth_method": "key", "key_base64": "Zm9v"}))
    monkeypatch.setattr("app.services.command_service.settings.COMMAND_CONFIG_DIR", str(tmp_path), raising=False)

    inv = InMemoryInventoryRepository(
        mappings={"type1": [BastionMapping(patterns=["taiwan-.*"], runner="r", bastion="b", bastion_ip="10.1.0.1")]}
    )
    svc = CommandService(_FakeRepo(), inventory_repo=inv)
    req = CommandExecutionRequest(
        command_name="noop", host="taiwan-taipei-my-cluster",
        host_type=HostType.CLUSTER, username="root",
    )
    ctx = await svc._prepare_execution("admin", "req-1", req)
    assert ctx.resolved_host.ip == "10.1.0.1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_cluster_prepare.py -v`
Expected: FAIL — `ValueError: CLUSTER resolver requires inventory_repo and slash_map` (the call doesn't yet pass `slash_map`).

- [ ] **Step 3: Pass `slash_map` in the resolver call**

In `app/services/command_service.py`, in `_prepare_execution`, update the `create_host_resolver(...)` call to add the `slash_map` kwarg:

```python
        resolver = create_host_resolver(
            req.host_type,
            inventory_repo=self.inventory_repo,
            node_type_map=settings.BASTION_NODE_TYPE_MAP,
            bastion_type=bastion_type,
            ip_label=ip_label,
            slash_map=settings.CLUSTER_SLASH_TYPE_MAP,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_cluster_prepare.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the existing command-service tests (no regression)**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_service.py tests/unit/test_host_resolver.py -v`
Expected: PASS (all existing tests still green).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_service.py tests/unit/test_command_cluster_prepare.py
git commit -m "feat(command): resolve cluster host_type via ClusterNameResolver"
```

---

## Task 4: `ClusterBastionResolution` model + `resolve_cluster_bastion` service

**Files:**
- Modify: `app/repositories/inventory_repository.py` (add model after `NodeBastionResolution`)
- Modify: `app/services/inventory_service.py` (`__init__` + new method)
- Modify: `app/core/dependencies.py` (`get_inventory_service` ~line 176-180)
- Test: `tests/unit/test_inventory_service.py` (extend)

**Interfaces:**
- Consumes: `cluster_type_from_name` (Task 1); `InventoryRepository.list_mappings`.
- Produces:
  - `ClusterBastionResolution(cluster_name, has_slash, bastion_type, matched_mapping: BastionMapping, matched_pattern)`.
  - `InventoryService.__init__(self, repo, node_type_map, slash_map)`.
  - `InventoryService.resolve_cluster_bastion(cluster_name: str) -> ClusterBastionResolution`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_inventory_service.py`:

```python
from app.repositories.inventory_repository import BastionMapping, ClusterBastionResolution
from app.services.inventory_service import InventoryService
from tests.fixtures.cluster import InMemoryInventoryRepository
import pytest
from app.core.exceptions import NotFoundException

_SLASH = {"no_slash": "type1", "with_slash": "type2"}


def _svc():
    repo = InMemoryInventoryRepository(
        mappings={
            "type1": [BastionMapping(patterns=["taiwan-.*"], runner="r1", bastion="b1", bastion_ip="10.1.0.1")],
            "type2": [BastionMapping(patterns=["taiwan-taipei/.*"], runner="r2", bastion="b2", bastion_ip="10.2.0.2")],
        }
    )
    return InventoryService(repo=repo, node_type_map={}, slash_map=_SLASH)


async def test_resolve_cluster_bastion_no_slash():
    res = await _svc().resolve_cluster_bastion("taiwan-taipei-my-cluster")
    assert isinstance(res, ClusterBastionResolution)
    assert res.bastion_type == "type1"
    assert res.has_slash is False
    assert res.matched_mapping.bastion_ip == "10.1.0.1"


async def test_resolve_cluster_bastion_with_slash():
    res = await _svc().resolve_cluster_bastion("taiwan-taipei/my-cluster")
    assert res.bastion_type == "type2"
    assert res.has_slash is True
    assert res.matched_mapping.bastion_ip == "10.2.0.2"


async def test_resolve_cluster_bastion_no_match():
    repo = InMemoryInventoryRepository(
        mappings={"type1": [BastionMapping(patterns=["nope-.*"], runner="r", bastion="b", bastion_ip="9.9.9.9")]}
    )
    svc = InventoryService(repo=repo, node_type_map={}, slash_map=_SLASH)
    with pytest.raises(NotFoundException):
        await svc.resolve_cluster_bastion("taiwan-taipei-x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_service.py -k cluster_bastion -v`
Expected: FAIL — `ImportError: cannot import name 'ClusterBastionResolution'`.

- [ ] **Step 3: Add the response model**

In `app/repositories/inventory_repository.py`, after `NodeBastionResolution`:

```python
class ClusterBastionResolution(BaseModel):
    cluster_name: str
    has_slash: bool
    bastion_type: str
    matched_mapping: BastionMapping
    matched_pattern: str
```

- [ ] **Step 4: Extend the service**

In `app/services/inventory_service.py`:

Add imports at top:
```python
from app.repositories.host_resolver import cluster_type_from_name
from app.repositories.inventory_repository import ClusterBastionResolution
```

Change `__init__` to accept `slash_map` with a default. The default keeps the
4 existing `InventoryService(...)` constructions in
`tests/integration/test_inventory_resolution.py` (lines 51/97/149/175) and
`tests/unit/test_inventory_service.py:39` working unchanged — only the new
cluster path reads `slash_map`, and those tests don't exercise it:
```python
    def __init__(
        self,
        repo: InventoryRepository,
        node_type_map: Dict[str, str],
        slash_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self._repo = repo
        self._node_type_map = node_type_map
        self._slash_map = slash_map or {}
```
Add `Optional` to the `typing` import at the top of the file
(`from typing import Dict, Optional`).

Add the method:
```python
    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        bastion_type, has_slash = cluster_type_from_name(cluster_name, self._slash_map)
        mappings = await self._repo.list_mappings(bastion_type)

        for mapping in mappings:
            for pattern in mapping.patterns:
                try:
                    matched = re.fullmatch(pattern, cluster_name)
                except re.error:
                    _logger.warning(
                        "Skipping invalid regex pattern %r in bastion mapping "
                        "(type=%s) — fix the mapping API data",
                        pattern, bastion_type,
                    )
                    continue
                if matched:
                    return ClusterBastionResolution(
                        cluster_name=cluster_name,
                        has_slash=has_slash,
                        bastion_type=bastion_type,
                        matched_mapping=mapping,
                        matched_pattern=pattern,
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={"cluster_name": cluster_name, "bastion_type": bastion_type},
        )
```

(`re`, `_logger`, `NotFoundException` are already imported in this module.)

- [ ] **Step 5: Update the DI factory**

In `app/core/dependencies.py`, `get_inventory_service`:

```python
async def get_inventory_service(
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> InventoryService:
    s = get_settings()
    return InventoryService(
        repo=repo,
        node_type_map=s.BASTION_NODE_TYPE_MAP,
        slash_map=s.CLUSTER_SLASH_TYPE_MAP,
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_service.py -v`
Expected: PASS (existing + 3 new).

- [ ] **Step 7: Commit**

```bash
git add app/repositories/inventory_repository.py app/services/inventory_service.py app/core/dependencies.py tests/unit/test_inventory_service.py
git commit -m "feat(inventory): resolve_cluster_bastion service + ClusterBastionResolution model"
```

---

## Task 5: Cluster bastion-resolution endpoint

**Files:**
- Modify: `app/api/v1/inventory.py` (add route)
- Modify: `app/api/router.py` (docstring route layout)
- Modify: `.env.test` (add `CLUSTER_SLASH_TYPE_MAP`)
- Test: `tests/integration/test_cluster_bastion_resolution_api.py` (create)

**Interfaces:**
- Consumes: `InventoryService.resolve_cluster_bastion` (Task 4); `get_inventory_service`; `get_current_user(["command_api"])`.
- Produces: `GET /api/v1/inventory/cluster/bastion-resolution?cluster_name=<name>` → `ApiResponse[ClusterBastionResolution]`.

- [ ] **Step 1: Set the test env var**

In `.env.test`, add a line:
```
CLUSTER_SLASH_TYPE_MAP={"no_slash": "type1", "with_slash": "type2"}
```

- [ ] **Step 2: Write the failing test**

Create `tests/integration/test_cluster_bastion_resolution_api.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_inventory_repository
from app.main import create_app
from app.repositories.inventory_repository import BastionMapping
from tests.fixtures.cluster import InMemoryInventoryRepository


def _token(client, account="test_admin"):
    r = client.post("/token", data={"username": account, "password": "secret"})
    return r.json()["access_token"]


@pytest.fixture
def client():
    inv = InMemoryInventoryRepository(
        mappings={
            "type1": [BastionMapping(patterns=["taiwan-.*"], runner="r1", bastion="b1", bastion_ip="10.1.0.1")],
            "type2": [BastionMapping(patterns=["taiwan-taipei/.*"], runner="r2", bastion="b2", bastion_ip="10.2.0.2")],
        }
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: inv
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_no_slash_resolves(client):
    t = _token(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "taiwan-taipei-my-cluster"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["bastion_type"] == "type1"
    assert data["has_slash"] is False
    assert data["matched_mapping"]["bastion_ip"] == "10.1.0.1"


def test_slash_resolves(client):
    t = _token(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "taiwan-taipei/my-cluster"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["bastion_type"] == "type2"


def test_no_match_returns_404(client):
    t = _token(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "nomatch-cluster"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 404, r.text


def test_requires_auth(client):
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "taiwan-taipei-my-cluster"},
    )
    assert r.status_code == 401
```

- [ ] **Step 3: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/integration/test_cluster_bastion_resolution_api.py -v`
Expected: FAIL — 404 route not found (or 405), because the endpoint doesn't exist.

- [ ] **Step 4: Add the route**

In `app/api/v1/inventory.py`, add `ClusterBastionResolution` to the import from `app.repositories.inventory_repository`, then add:

```python
@router.get(
    "/cluster/bastion-resolution",
    response_model=ApiResponse[ClusterBastionResolution],
    summary="Resolve a cluster name to a bastion runner (slash-presence selects type)",
)
async def get_cluster_bastion_resolution(
    request: Request,
    cluster_name: str = Query(..., description="Cluster name; a '/' selects the with_slash bastion_type"),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
    service: InventoryService = Depends(get_inventory_service),
) -> ApiResponse[ClusterBastionResolution]:
    data = await service.resolve_cluster_bastion(cluster_name)
    return ApiResponse(data=data, request_id=_request_id(request))
```

- [ ] **Step 5: Update the router docstring**

In `app/api/router.py`, add to the route-layout docstring list:
```
  GET  /api/v1/inventory/cluster/bastion-resolution → Cluster-name-to-bastion resolution
```

- [ ] **Step 6: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/integration/test_cluster_bastion_resolution_api.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Commit**

```bash
git add app/api/v1/inventory.py app/api/router.py .env.test tests/integration/test_cluster_bastion_resolution_api.py
git commit -m "feat(inventory): GET /inventory/cluster/bastion-resolution endpoint"
```

---

## Task 6: `list_states` repository method

**Files:**
- Modify: `app/repositories/command_state_repository.py`
- Test: `tests/unit/test_command_state_list.py` (create)

**Interfaces:**
- Consumes: `CommandState`, `CommandStatus`.
- Produces: `CommandStateRepository.list_states(statuses: Optional[set[CommandStatus]] = None) -> list[CommandState]` — scans `command:*`, parses each, skips unparseable (logs warning), filters by `statuses` when given.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_state_list.py`:

```python
import pytest

from app.domain.command import CommandState, CommandStatus
from app.repositories.command_state_repository import CommandStateRepository


class _FakeRedis:
    """Minimal async Redis stand-in for scan_iter + get."""

    def __init__(self, data: dict[str, str]):
        self._data = data

    async def scan_iter(self, match=None):
        import fnmatch
        for k in list(self._data.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    async def get(self, key):
        return self._data.get(key)


def _state(cid, status):
    return CommandState(
        command_id=cid, status=status, host="h", resolved_ip="1.1.1.1",
        port=22, username="root", ssh_config="default", request_id="r",
        exec_command="true", killable=False,
    ).model_dump_json()


async def test_list_states_filters_by_status():
    redis = _FakeRedis({
        "command:a": _state("a", CommandStatus.RUNNING),
        "command:b": _state("b", CommandStatus.KILLING),
        "command:c": _state("c", CommandStatus.SUCCESS),
        "other:x": "garbage",
    })
    repo = CommandStateRepository(redis)
    result = await repo.list_states({CommandStatus.RUNNING, CommandStatus.KILLING})
    ids = sorted(s.command_id for s in result)
    assert ids == ["a", "b"]


async def test_list_states_skips_unparseable():
    redis = _FakeRedis({
        "command:a": _state("a", CommandStatus.RUNNING),
        "command:bad": "not-json",
    })
    repo = CommandStateRepository(redis)
    result = await repo.list_states()
    assert [s.command_id for s in result] == ["a"]


async def test_list_states_no_filter_returns_all_parseable():
    redis = _FakeRedis({
        "command:a": _state("a", CommandStatus.RUNNING),
        "command:c": _state("c", CommandStatus.SUCCESS),
    })
    repo = CommandStateRepository(redis)
    result = await repo.list_states()
    assert sorted(s.command_id for s in result) == ["a", "c"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_state_list.py -v`
Expected: FAIL — `AttributeError: 'CommandStateRepository' object has no attribute 'list_states'`.

- [ ] **Step 3: Implement `list_states`**

In `app/repositories/command_state_repository.py`, add imports + method. Add at top:
```python
import logging
from typing import Callable, Coroutine, Optional
from app.domain.command import CommandState, CommandStatus

_logger = logging.getLogger(__name__)
```
(Keep existing imports; add `Optional` and `CommandStatus` if not present, and the `logging`/`_logger` lines.)

Add the method to the class:
```python
    async def list_states(
        self, statuses: Optional[set[CommandStatus]] = None
    ) -> list[CommandState]:
        """Scan all command:* keys and return the parsed states.

        Cursor-based scan_iter (not KEYS) so it is safe on a shared Redis.
        Unparseable records are skipped with a warning. When `statuses` is
        given, only states with a matching status are returned.
        """
        out: list[CommandState] = []
        async for key in self.redis.scan_iter(match=f"{self.PREFIX}:*"):
            raw = await self.redis.get(key)
            if not raw:
                continue
            try:
                state = CommandState.model_validate_json(raw)
            except Exception:
                _logger.warning("Skipping unparseable command state at key %s", key)
                continue
            if statuses is None or state.status in statuses:
                out.append(state)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_state_list.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/repositories/command_state_repository.py tests/unit/test_command_state_list.py
git commit -m "feat(command): list_states Redis scan for in-flight commands"
```

---

## Task 7: `list_running_commands` service + `RunningCommandsResponse` model

**Files:**
- Modify: `app/domain/command.py` (add `RunningCommandsResponse`)
- Modify: `app/services/command_service.py` (add method)
- Test: `tests/unit/test_command_service_running.py` (create)

**Interfaces:**
- Consumes: `CommandStateRepository.list_states` (Task 6); `CommandStatus`.
- Produces:
  - `RunningCommandsResponse(count: int, commands: List[CommandState])`.
  - `CommandService.list_running_commands(statuses: Optional[set[CommandStatus]] = None) -> list[CommandState]` — defaults to `{RUNNING, KILLING}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_service_running.py`:

```python
from app.domain.command import CommandStatus
from app.services.command_service import CommandService


class _Repo:
    def __init__(self):
        self.called_with = "unset"

    async def list_states(self, statuses=None):
        self.called_with = statuses
        return []


async def test_default_uses_running_and_killing():
    repo = _Repo()
    svc = CommandService(repo)
    await svc.list_running_commands()
    assert repo.called_with == {CommandStatus.RUNNING, CommandStatus.KILLING}


async def test_explicit_statuses_passed_through():
    repo = _Repo()
    svc = CommandService(repo)
    await svc.list_running_commands({CommandStatus.SUCCESS})
    assert repo.called_with == {CommandStatus.SUCCESS}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_service_running.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_running_commands'`.

- [ ] **Step 3: Add the response model**

In `app/domain/command.py`, after `CommandTraceResponse`:

```python
class RunningCommandsResponse(BaseModel):
    count: int
    commands: List[CommandState]
```

- [ ] **Step 4: Add the service method**

In `app/services/command_service.py`, add a method to `CommandService`:

```python
    async def list_running_commands(
        self, statuses: Optional[set[CommandStatus]] = None
    ) -> List[CommandState]:
        """Return command states currently in-flight across all pods.

        Defaults to non-terminal states (RUNNING + KILLING) when no explicit
        status set is given. Reads from Redis so it sees commands started on
        other pods.
        """
        if statuses is None:
            statuses = {CommandStatus.RUNNING, CommandStatus.KILLING}
        return await self.repo.list_states(statuses)
```

(`CommandStatus`, `CommandState`, `Optional`, `List` are already imported in the module.)

- [ ] **Step 5: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_service_running.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add app/domain/command.py app/services/command_service.py tests/unit/test_command_service_running.py
git commit -m "feat(command): list_running_commands service + RunningCommandsResponse"
```

---

## Task 8: `admin_api` scope + `/command/running` endpoint

**Files:**
- Modify: `data/users.json` (add `admin_api` to `admin`)
- Modify: `tests/fixtures/users.json` (add `admin_api` to `test_admin`; add a `command_api`-only user `test_command`)
- Modify: `app/api/v1/command.py` (add route + imports)
- Modify: `app/api/router.py` (docstring route layout)
- Test: `tests/integration/test_command_running_api.py` (create)

**Interfaces:**
- Consumes: `CommandService.list_running_commands` (Task 7); `RunningCommandsResponse` (Task 7); `get_current_user(["admin_api"])`; `get_command_service`.
- Produces: `GET /api/v1/command/running?status=<optional CommandStatus>` → `ApiResponse[RunningCommandsResponse]`, gated by `admin_api`.

- [ ] **Step 1: Add scopes to user data**

In `data/users.json`, add `"admin_api"` to the `admin` account's `scopes` list (alongside `deploy_api`, `vm_api`, `command_api`).

In `tests/fixtures/users.json`, add `"admin_api"` to `test_admin`'s scopes, and append a new user:
```json
    {
        "account": "test_command",
        "hashed_password": "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
        "scopes": [
            "command_api"
        ]
    }
```
(Same hash as the other fixture users → password `secret`.)

- [ ] **Step 2: Write the failing test**

Create `tests/integration/test_command_running_api.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_command_service
from app.domain.command import CommandState, CommandStatus
from app.main import create_app


def _token(client, account):
    r = client.post("/token", data={"username": account, "password": "secret"})
    return r.json()["access_token"]


def _state(cid, status):
    return CommandState(
        command_id=cid, status=status, host="h", resolved_ip="1.1.1.1",
        port=22, username="root", ssh_config="default", request_id="r",
        exec_command="true", killable=False,
    )


class _FakeService:
    async def list_running_commands(self, statuses=None):
        all_states = [
            _state("a", CommandStatus.RUNNING),
            _state("b", CommandStatus.KILLING),
            _state("c", CommandStatus.SUCCESS),
        ]
        if statuses is None:
            statuses = {CommandStatus.RUNNING, CommandStatus.KILLING}
        return [s for s in all_states if s.status in statuses]


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_command_service] = lambda: _FakeService()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_admin_lists_non_terminal(client):
    t = _token(client, "test_admin")
    r = client.get("/api/v1/command/running", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["count"] == 2
    assert sorted(c["command_id"] for c in data["commands"]) == ["a", "b"]


def test_status_filter(client):
    t = _token(client, "test_admin")
    r = client.get(
        "/api/v1/command/running",
        params={"status": "success"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["count"] == 1
    assert data["commands"][0]["command_id"] == "c"


def test_command_api_only_user_forbidden(client):
    t = _token(client, "test_command")
    r = client.get("/api/v1/command/running", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 403, r.text


def test_invalid_status_422(client):
    t = _token(client, "test_admin")
    r = client.get(
        "/api/v1/command/running",
        params={"status": "bogus"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 422
```

- [ ] **Step 3: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/integration/test_command_running_api.py -v`
Expected: FAIL — 404 (route missing).

- [ ] **Step 4: Add the route**

In `app/api/v1/command.py`:

Extend the domain import to include `CommandStatus` (already imported) and `RunningCommandsResponse`:
```python
from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    CommandStatus, CommandTraceResponse,
    UserCommandWhitelist, CommandWhitelistConfig,
    RunningCommandsResponse,
)
```

Add `Optional` import from typing at top:
```python
from typing import Optional
```

Add the route (place it before the `/execution/{command_id}` route so `running` isn't shadowed by the `{command_id}` path param — actually they don't collide because this is `/running` not `/execution/...`, but keep it grouped near the other GETs):

```python
@router.get(
    "/running",
    response_model=ApiResponse[RunningCommandsResponse],
    summary="List in-flight commands across all pods (admin only)",
    description="Returns commands not yet in a terminal state (default running+killing). "
                "Admin-gated so operators can decide whether an upgrade is safe.",
)
async def list_running_commands_endpoint(
    request: Request,
    status: Optional[CommandStatus] = Query(
        default=None,
        description="Optional single status filter; default returns running + killing.",
    ),
    current_user: User = Depends(get_current_user(["admin_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[RunningCommandsResponse]:
    statuses = {status} if status is not None else None
    states = await svc.list_running_commands(statuses)
    data = RunningCommandsResponse(count=len(states), commands=states)
    return ApiResponse(data=data, request_id=_request_id(request))
```

- [ ] **Step 5: Update the router docstring**

In `app/api/router.py`, add to the route-layout docstring:
```
  GET  /api/v1/command/running             → List in-flight commands (admin_api)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/integration/test_command_running_api.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Commit**

```bash
git add data/users.json tests/fixtures/users.json app/api/v1/command.py app/api/router.py tests/integration/test_command_running_api.py
git commit -m "feat(command): GET /command/running admin endpoint + admin_api scope"
```

---

## Task 9: Full suite green + docs touch-up

**Files:**
- Modify: `data/allow-commands-admin.json` (optional: add a `cluster`-host example command if the repo documents host_type usage there — only if such examples exist)
- Verify only; no new code.

- [ ] **Step 1: Run the full test suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: PASS (all tests, including pre-existing, green).

- [ ] **Step 2: If any pre-existing test broke**

Use `superpowers:systematic-debugging`. With `slash_map` defaulted to `None`/`{}`
(Task 4) and `create_host_resolver`'s `slash_map` param optional (Task 2), the
known callers stay green:
- `tests/integration/test_inventory_resolution.py` (lines 51/97/149/175) and
  `tests/unit/test_inventory_service.py:39` construct `InventoryService` without
  `slash_map` — fine, the param is optional.
- `app/core/dependencies.py` is updated in Task 4 to pass `slash_map`.
If something else broke, debug it before continuing.

- [ ] **Step 3: Sanity-check the running endpoint against real Redis (manual, optional)**

If Redis is available (`make redis-up`), start the dev server and confirm:
```bash
curl -s -H "Authorization: Bearer <admin token>" http://localhost:8001/api/v1/command/running | jq
```
Expected: `{"data": {"count": 0, "commands": []}, "request_id": "..."}` on an idle system.

- [ ] **Step 4: Final commit (if any fixups were made)**

```bash
git add -A
git commit -m "test: fix InventoryService constructions for slash_map; full suite green"
```

---

## Self-Review Notes

- **Spec coverage:** A (Tasks 1-3), B (Tasks 4-5), C (Tasks 6-8). Shared helper `cluster_type_from_name` used by both resolver (Task 2) and service (Task 4) — no drift. Config `CLUSTER_SLASH_TYPE_MAP` (Task 1), `.env.test` (Task 5). `admin_api` scope (Task 8). `scan_iter` not `KEYS` (Task 6).
- **Type consistency:** `cluster_type_from_name -> (bastion_type, has_slash)` used identically in Tasks 2/4. `ClusterBastionResolution` fields match between model (Task 4) and tests. `list_states(statuses)` signature matches between repo (Task 6), service (Task 7), and the fake in Task 8.
- **Note on `InventoryService.__init__`:** Task 4 adds `slash_map` as an *optional* param (default `{}`), so the existing constructions in `tests/integration/test_inventory_resolution.py` (4×) and `tests/unit/test_inventory_service.py` keep working without edits; `app/core/dependencies.py` is updated in Task 4 to pass the real map.
