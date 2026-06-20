# Inventory Bastion Resolution Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /api/v1/inventory/nodes/{node_name}/bastion-resolution` that returns a single debug-friendly response showing which bastion runner a node maps to, including all intermediate resolution data.

**Architecture:** New `InventoryService` in the service layer orchestrates two existing `InventoryRepository` calls (`lookup_by_name` + `list_mappings`) and applies the same regex pattern-matching logic as `ClusterBastionHostResolver`, but returns all intermediate data instead of just the resolved IP. The router calls the service via a new DI factory; the new response model `NodeBastionResolution` is added to `inventory_repository.py` and composes existing models.

**Tech Stack:** FastAPI, Pydantic v2, pytest, `unittest.mock` (AsyncMock), existing `InMemoryInventoryRepository` fixture from `tests/fixtures/cluster.py`.

## Global Constraints

- Run all commands from `deploy-service/` directory
- Test command: `APP_ENV=test uv run pytest <path> -v`
- `asyncio_mode = "auto"` is set in `pyproject.toml` — no `@pytest.mark.asyncio` decorator needed
- Auth scope for all new endpoints: `command_api`
- All successful responses use `ApiResponse[T]` envelope: `{"data": <T>, "request_id": "..."}`
- Error responses: `NotFoundException` → 404, `CommandExecutionException` → existing handler (500 with `COMMAND_EXECUTION_ERROR` code)
- Pattern matching uses `re.fullmatch(pattern, cluster_name)` — full-string match only
- Invalid regex patterns in mapping data are skipped with a `logger.warning`, not raised

---

### Task 1: Add `NodeBastionResolution` model to `inventory_repository.py`

**Files:**
- Modify: `app/repositories/inventory_repository.py`
- Test: `tests/unit/test_inventory_service.py` (created here, extended in Task 2)

**Interfaces:**
- Produces: `NodeBastionResolution` Pydantic model with fields:
  - `node_type: str`
  - `node: NodeInfo`
  - `cluster: ClusterRef`
  - `bastion_type: str`
  - `bastion_type_source: Literal["config", "query_param"]`
  - `matched_mapping: BastionMapping`
  - `matched_pattern: str`

- [ ] **Step 1: Write a failing import test**

Create `tests/unit/test_inventory_service.py`:

```python
"""Unit tests for InventoryService."""
from __future__ import annotations

from app.repositories.inventory_repository import NodeBastionResolution
```

- [ ] **Step 2: Run test to verify it fails**

```bash
APP_ENV=test uv run pytest tests/unit/test_inventory_service.py -v
```

Expected: `ImportError: cannot import name 'NodeBastionResolution'`

- [ ] **Step 3: Add `NodeBastionResolution` to `inventory_repository.py`**

Add this import at the top of `app/repositories/inventory_repository.py` (after the existing imports):

```python
from typing import Literal
```

Add this model after the `BastionMapping` class definition (before the `# ── Abstract interfaces` section):

```python
class NodeBastionResolution(BaseModel):
    node_type: str
    node: NodeInfo
    cluster: ClusterRef
    bastion_type: str
    bastion_type_source: Literal["config", "query_param"]
    matched_mapping: BastionMapping
    matched_pattern: str
```

- [ ] **Step 4: Run test to verify import succeeds**

```bash
APP_ENV=test uv run pytest tests/unit/test_inventory_service.py -v
```

Expected: PASSED (1 test collected, import works)

- [ ] **Step 5: Commit**

```bash
git add app/repositories/inventory_repository.py tests/unit/test_inventory_service.py
git commit -m "feat: add NodeBastionResolution model to inventory_repository"
```

---

### Task 2: Implement `InventoryService`

**Files:**
- Create: `app/services/inventory_service.py`
- Modify: `tests/unit/test_inventory_service.py` (extend with service tests)

**Interfaces:**
- Consumes from Task 1: `NodeBastionResolution`, `ClusterNodeInfo`, `ClusterRef`, `NodeInfo`, `BastionMapping` from `app.repositories.inventory_repository`; `InventoryRepository` ABC from same module
- Consumes from existing code: `NotFoundException`, `CommandExecutionException` from `app.core.exceptions`; `InMemoryInventoryRepository` from `tests.fixtures.cluster`
- Produces: `InventoryService` class with `__init__(self, repo: InventoryRepository, node_type_map: Dict[str, str])` and `async def resolve_node_bastion(self, node_name: str, bastion_type_override: Optional[str] = None) -> NodeBastionResolution`

- [ ] **Step 1: Write all failing unit tests**

Replace the contents of `tests/unit/test_inventory_service.py` with:

```python
"""Unit tests for InventoryService."""
from __future__ import annotations

import pytest

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)
from app.services.inventory_service import InventoryService
from tests.fixtures.cluster import InMemoryInventoryRepository


_NODE_TYPE_MAP = {"baremetal": "type1", "virtual-machine": "type2"}


def _repo(
    cluster_name: str = "type1-cluster-c1",
    node_type: str = "baremetal",
    mappings: dict | None = None,
) -> InMemoryInventoryRepository:
    return InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type=node_type,
                node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.0.1.5/24"}),
                cluster=ClusterRef(id="1", name=cluster_name),
            )
        },
        mappings=mappings or {},
    )


def _service(repo, node_type_map=None) -> InventoryService:
    return InventoryService(repo=repo, node_type_map=node_type_map or _NODE_TYPE_MAP)


# ── Happy path ────────────────────────────────────────────────────────────────

async def test_resolve_uses_config_node_type_map():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-c.*"],
                runner="runner1",
                bastion="bastion1",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    svc = _service(_repo(cluster_name="type1-cluster-c1", mappings=mappings))
    result = await svc.resolve_node_bastion("node1")

    assert isinstance(result, NodeBastionResolution)
    assert result.node_type == "baremetal"
    assert result.node.name == "node1"
    assert result.cluster.name == "type1-cluster-c1"
    assert result.bastion_type == "type1"
    assert result.bastion_type_source == "config"
    assert result.matched_mapping.runner == "runner1"
    assert result.matched_pattern == "type1-cluster-c.*"


async def test_resolve_bastion_type_override_sets_query_param_source():
    mappings = {
        "override-type": [
            BastionMapping(
                patterns=[".*"],
                runner="override-runner",
                bastion="override-bastion",
                bastion_ip="10.9.9.9",
            )
        ]
    }
    # node_type=baremetal would map to type1 via config, but override forces override-type
    svc = _service(_repo(cluster_name="any-cluster", mappings=mappings))
    result = await svc.resolve_node_bastion("node1", bastion_type_override="override-type")

    assert result.bastion_type == "override-type"
    assert result.bastion_type_source == "query_param"
    assert result.matched_mapping.runner == "override-runner"


async def test_resolve_first_matching_pattern_wins():
    """First pattern in first mapping entry that matches cluster_name wins."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="runner1",
                bastion="bastion1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                patterns=["type1-kind"],
                runner="runner2",
                bastion="bastion2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    svc = _service(_repo(cluster_name="type1-cluster-c1", mappings=mappings))
    result = await svc.resolve_node_bastion("node1")

    assert result.matched_mapping.runner == "runner1"
    assert result.matched_pattern == "type1-cluster-(c1|c2|c3)"


async def test_resolve_second_entry_matches_when_first_does_not():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)"],
                runner="runner1",
                bastion="bastion1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                patterns=["type1-kind"],
                runner="runner2",
                bastion="bastion2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    svc = _service(_repo(cluster_name="type1-kind", mappings=mappings))
    result = await svc.resolve_node_bastion("node1")

    assert result.matched_mapping.runner == "runner2"
    assert result.matched_pattern == "type1-kind"


# ── Error cases ───────────────────────────────────────────────────────────────

async def test_resolve_node_not_found_raises_not_found():
    svc = _service(InMemoryInventoryRepository(nodes={}, mappings={}))
    with pytest.raises(NotFoundException):
        await svc.resolve_node_bastion("missing-node")


async def test_resolve_unknown_node_type_raises_command_execution_exception():
    mappings = {
        "type1": [BastionMapping(patterns=[".*"], runner="r", bastion="b", bastion_ip="1.1.1.1")]
    }
    svc = _service(
        _repo(node_type="unknown-type", mappings=mappings),
        node_type_map={"baremetal": "type1"},
    )
    with pytest.raises(CommandExecutionException) as exc_info:
        await svc.resolve_node_bastion("node1")
    assert "unknown-type" in str(exc_info.value)


async def test_resolve_no_pattern_matches_raises_not_found():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    svc = _service(_repo(cluster_name="type1-cluster-c99", mappings=mappings))
    with pytest.raises(NotFoundException) as exc_info:
        await svc.resolve_node_bastion("node1")
    assert "type1-cluster-c99" in str(exc_info.value)


async def test_resolve_invalid_regex_pattern_is_skipped(caplog):
    """An invalid regex in mapping data is logged and skipped; next pattern tried."""
    import logging
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["[invalid-regex", ".*"],  # first is invalid, second matches
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    svc = _service(_repo(cluster_name="any-cluster", mappings=mappings))
    with caplog.at_level(logging.WARNING):
        result = await svc.resolve_node_bastion("node1")
    assert result.matched_pattern == ".*"
    assert any("invalid" in record.message.lower() or "regex" in record.message.lower()
               for record in caplog.records)
```

- [ ] **Step 2: Run tests to verify they all fail**

```bash
APP_ENV=test uv run pytest tests/unit/test_inventory_service.py -v
```

Expected: `ImportError: cannot import name 'InventoryService' from 'app.services.inventory_service'`

- [ ] **Step 3: Create `app/services/inventory_service.py`**

```python
"""Inventory resolution service."""
from __future__ import annotations

import logging
import re
from typing import Dict, Optional

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.repositories.inventory_repository import (
    BastionMapping,
    InventoryRepository,
    NodeBastionResolution,
)

_logger = logging.getLogger(__name__)


class InventoryService:
    def __init__(
        self,
        repo: InventoryRepository,
        node_type_map: Dict[str, str],
    ) -> None:
        self._repo = repo
        self._node_type_map = node_type_map

    async def resolve_node_bastion(
        self,
        node_name: str,
        bastion_type_override: Optional[str] = None,
    ) -> NodeBastionResolution:
        node_info = await self._repo.lookup_by_name(node_name)
        cluster_name = node_info.cluster.name

        if bastion_type_override:
            bastion_type = bastion_type_override
            bastion_type_source = "query_param"
        else:
            node_type = node_info.node_type
            bastion_type = self._node_type_map.get(node_type)
            if bastion_type is None:
                known = ", ".join(f"{k!r}→{v!r}" for k, v in self._node_type_map.items())
                raise CommandExecutionException(
                    f"node_type '{node_type}' has no bastion mapping. "
                    f"Known mappings: {{{known}}}. "
                    "Update BASTION_NODE_TYPE_MAP to include this node_type.",
                    detail={"node_type": node_type, "node_type_map": self._node_type_map},
                )
            bastion_type_source = "config"

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
                    return NodeBastionResolution(
                        node_type=node_info.node_type,
                        node=node_info.node,
                        cluster=node_info.cluster,
                        bastion_type=bastion_type,
                        bastion_type_source=bastion_type_source,
                        matched_mapping=mapping,
                        matched_pattern=pattern,
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={
                "node_name": node_name,
                "cluster_name": cluster_name,
                "bastion_type": bastion_type,
            },
        )
```

- [ ] **Step 4: Run tests to verify they all pass**

```bash
APP_ENV=test uv run pytest tests/unit/test_inventory_service.py -v
```

Expected: 8 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add app/services/inventory_service.py tests/unit/test_inventory_service.py
git commit -m "feat: add InventoryService with resolve_node_bastion"
```

---

### Task 3: Wire DI factory and add router endpoint

**Files:**
- Modify: `app/core/dependencies.py`
- Modify: `app/api/v1/inventory.py`
- Test: `tests/integration/test_inventory_resolution.py` (new file)

**Interfaces:**
- Consumes from Task 2: `InventoryService` from `app.services.inventory_service`
- Consumes from Task 1: `NodeBastionResolution` from `app.repositories.inventory_repository`
- Consumes from existing: `get_inventory_repository` from `app.core.dependencies`; `get_settings` from `app.core.config`; `InMemoryInventoryRepository` from `tests.fixtures.cluster`; `get_inventory_service` (produced here) for dependency override in tests
- Produces: `GET /api/v1/inventory/nodes/{node_name}/bastion-resolution` endpoint

- [ ] **Step 1: Write failing integration tests**

Create `tests/integration/test_inventory_resolution.py`:

```python
"""Integration tests for GET /api/v1/inventory/nodes/{node_name}/bastion-resolution."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_inventory_repository, get_inventory_service
from app.main import create_app
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    ClusterRef,
    NodeInfo,
)
from app.services.inventory_service import InventoryService
from tests.fixtures.cluster import InMemoryInventoryRepository


_NODE_TYPE_MAP = {"baremetal": "type1"}

_INV_REPO = InMemoryInventoryRepository(
    nodes={
        "node1": ClusterNodeInfo(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.0.1.5/24"}),
            cluster=ClusterRef(id="1", name="type1-cluster-c1"),
        ),
    },
    mappings={
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-c.*"],
                runner="type1-runner",
                bastion="type1-bastion",
                bastion_ip="10.223.192.40",
            )
        ]
    },
)


def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


@pytest.fixture
def resolution_client():
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: _INV_REPO
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=_INV_REPO, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Happy path ────────────────────────────────────────────────────────────────

def test_resolution_success(resolution_client):
    token = _get_token(resolution_client)
    resp = resolution_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["node_type"] == "baremetal"
    assert data["node"]["name"] == "node1"
    assert data["cluster"]["name"] == "type1-cluster-c1"
    assert data["bastion_type"] == "type1"
    assert data["bastion_type_source"] == "config"
    assert data["matched_mapping"]["runner"] == "type1-runner"
    assert data["matched_mapping"]["bastion_ip"] == "10.223.192.40"
    assert data["matched_pattern"] == "type1-cluster-c.*"


def test_resolution_with_bastion_type_override(resolution_client):
    # Add override-type mapping to the in-memory repo inline
    override_repo = InMemoryInventoryRepository(
        nodes=_INV_REPO._nodes,
        mappings={
            **_INV_REPO._mappings,
            "override-type": [
                BastionMapping(
                    patterns=[".*"],
                    runner="override-runner",
                    bastion="override-bastion",
                    bastion_ip="10.9.9.9",
                )
            ],
        },
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: override_repo
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=override_repo, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        token = _get_token(c)
        resp = c.get(
            "/api/v1/inventory/nodes/node1/bastion-resolution",
            params={"bastion_type": "override-type"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["bastion_type"] == "override-type"
    assert data["bastion_type_source"] == "query_param"
    assert data["matched_mapping"]["runner"] == "override-runner"


# ── Error cases ───────────────────────────────────────────────────────────────

def test_resolution_node_not_found_returns_404(resolution_client):
    token = _get_token(resolution_client)
    resp = resolution_client.get(
        "/api/v1/inventory/nodes/missing-node/bastion-resolution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_resolution_no_pattern_match_returns_404(resolution_client):
    no_match_repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(id="1", name="node1", labels={}),
                cluster=ClusterRef(id="1", name="unmatched-cluster"),
            )
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["type1-cluster-c.*"],
                    runner="r",
                    bastion="b",
                    bastion_ip="10.0.0.1",
                )
            ]
        },
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: no_match_repo
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=no_match_repo, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        token = _get_token(c)
        resp = c.get(
            "/api/v1/inventory/nodes/node1/bastion-resolution",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404, resp.text


def test_resolution_no_token_returns_401(resolution_client):
    resp = resolution_client.get("/api/v1/inventory/nodes/node1/bastion-resolution")
    assert resp.status_code == 401


def test_resolution_wrong_scope_returns_403(resolution_client):
    # test_deployer only has deploy_api scope, not command_api
    resp = resolution_client.post(
        "/token", data={"username": "test_deployer", "password": "secret"}
    )
    token = resp.json()["access_token"]
    resp = resolution_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
APP_ENV=test uv run pytest tests/integration/test_inventory_resolution.py -v
```

Expected: `ImportError: cannot import name 'get_inventory_service' from 'app.core.dependencies'`

- [ ] **Step 3: Add `get_inventory_service` DI factory to `app/core/dependencies.py`**

Add these imports near the top of the existing import block at the bottom of `dependencies.py` (after the existing service imports):

```python
from app.services.inventory_service import InventoryService
```

Add the factory function after `get_inventory_repository`:

```python
async def get_inventory_service(
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> InventoryService:
    s = get_settings()
    return InventoryService(repo=repo, node_type_map=s.BASTION_NODE_TYPE_MAP)
```

- [ ] **Step 4: Add the new route to `app/api/v1/inventory.py`**

Add `Optional` to the existing imports at the top:

```python
from typing import Annotated, List, Optional
```

Add the new import for `NodeBastionResolution` and `InventoryService` and `get_inventory_service`:

```python
from app.core.dependencies import (
    get_current_user,
    get_inventory_repository,
    get_inventory_service,
)
from app.domain.models import ApiResponse, User
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    InventoryRepository,
    NodeBastionResolution,
)
from app.services.inventory_service import InventoryService
```

Add the new endpoint after the `get_mappings` function:

```python
@router.get(
    "/nodes/{node_name}/bastion-resolution",
    response_model=ApiResponse[NodeBastionResolution],
    summary="Resolve node name to bastion runner",
)
async def get_node_bastion_resolution(
    request: Request,
    node_name: str,
    bastion_type: Optional[str] = Query(default=None, description="Override bastion type (default: derived from node_type via BASTION_NODE_TYPE_MAP config)"),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
    service: InventoryService = Depends(get_inventory_service),
) -> ApiResponse[NodeBastionResolution]:
    data = await service.resolve_node_bastion(node_name, bastion_type_override=bastion_type)
    return ApiResponse(data=data, request_id=_request_id(request))
```

- [ ] **Step 5: Run integration tests to verify they all pass**

```bash
APP_ENV=test uv run pytest tests/integration/test_inventory_resolution.py -v
```

Expected: 5 tests PASSED

- [ ] **Step 6: Run full test suite to verify no regressions**

```bash
APP_ENV=test uv run pytest tests/ -v
```

Expected: All existing tests still pass

- [ ] **Step 7: Commit**

```bash
git add app/core/dependencies.py app/api/v1/inventory.py tests/integration/test_inventory_resolution.py
git commit -m "feat: add GET /inventory/nodes/{node_name}/bastion-resolution endpoint"
```
