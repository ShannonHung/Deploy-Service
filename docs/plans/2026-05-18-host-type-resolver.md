# Host Type Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `host_type` field to `POST /api/v1/command/execution` so callers can target a host by `ip`, `hostname` (resolved via Inventory API), or `bastion` (resolves to the bastion's IP), and fix error handling so user-input / policy failures return proper 4xx instead of `200 + failed body`.

**Architecture:** Introduce a `HostResolver` strategy interface with three concrete implementations selected by a factory. `HostnameHostResolver` and `BastionHostResolver` use a new `InventoryRepository` (`HttpInventoryRepository` against an Inventory API) to look up the target IP. A standalone fake Inventory API ships under `deploy-service/fake-api/` with a JSON data file the developer edits by hand. `CommandService` becomes async-prepare-aware, uses the resolved IP for allow/deny, SSH connect, and cross-pod kill, and raises typed exceptions instead of swallowing them.

**Tech Stack:** FastAPI, Pydantic v2, asyncssh, httpx (already a transitive dep — promoted to direct), Redis (via existing `CommandStateRepository`), pytest + pytest-asyncio.

**Spec:** `deploy-service/docs/specs/2026-05-18-host-type-resolver-design.md`

---

## File Map

**Create:**
- `deploy-service/app/repositories/inventory_repository.py` — `InventoryRepository` ABC, models, `HttpInventoryRepository`.
- `deploy-service/app/repositories/host_resolver.py` — `HostResolver` ABC, three resolvers, `create_host_resolver` factory, `ResolvedHost` model.
- `deploy-service/fake-api/__init__.py` — empty marker so uvicorn can import `fake-api.main`.
- `deploy-service/fake-api/main.py` — single-file FastAPI fake.
- `deploy-service/fake-api/data/inventory.json` — sample inventory records.
- `deploy-service/tests/unit/test_host_resolver.py`
- `deploy-service/tests/unit/test_inventory_repository.py`
- `deploy-service/tests/unit/test_command_service_errors.py`
- `deploy-service/tests/integration/test_command_host_type.py`
- `deploy-service/tests/fixtures/inventory.py` — `InMemoryInventoryRepository` test double.

**Modify:**
- `deploy-service/app/domain/command.py` — add `HostType` enum, `host_type` field on request, `host_type`/`resolved_ip` on `CommandState`.
- `deploy-service/app/core/exceptions.py` — add `ServiceUnavailableException` (503).
- `deploy-service/app/core/config.py` — add three Inventory settings.
- `deploy-service/app/core/dependencies.py` — add `get_inventory_repository`, update `get_command_service`.
- `deploy-service/app/services/command_service.py` — async `_prepare_execution`, raise typed exceptions, use resolved IP, persist new state fields, cross-pod kill via `resolved_ip`.
- `deploy-service/app/api/v1/command.py` — drop redundant `try/except`, kill endpoint raises `ConflictException` instead of returning 200 failed body, GET poll raises `NotFoundException`.
- `deploy-service/Makefile` — add `inventory-api` target and help line.
- `deploy-service/.env.dev` — add Inventory env vars.
- `deploy-service/.env.test` — add Inventory env vars.
- `deploy-service/pyproject.toml` — promote `httpx` to a direct dependency.
- `deploy-service/tests/integration/test_ssh_command_api.py` — update expected status codes for the cases this plan changes (regex fail → 400, etc.).
- `deploy-service/ssh-command.md` — new "Host resolution" section, updated error table, migration notes.

---

## Task 1: Add `ServiceUnavailableException`

**Files:**
- Modify: `deploy-service/app/core/exceptions.py` (after `ConflictException`)
- Test: `deploy-service/tests/unit/test_exceptions.py` (new — single file for this small check)

- [ ] **Step 1: Write the failing test**

Create `deploy-service/tests/unit/test_exceptions.py`:

```python
from app.core.exceptions import ServiceUnavailableException


def test_service_unavailable_exception_attributes():
    exc = ServiceUnavailableException("capacity full")
    assert exc.http_status == 503
    assert exc.error_code == "SERVICE_UNAVAILABLE"
    assert exc.message == "capacity full"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_exceptions.py -v
```
Expected: ImportError / cannot import `ServiceUnavailableException`.

- [ ] **Step 3: Add the exception**

In `deploy-service/app/core/exceptions.py`, after the `ConflictException` class (around line 158), insert:

```python
class ServiceUnavailableException(BaseAppException):
    """Raised when the service temporarily cannot accept the request
    (e.g. running-command pool is full)."""

    http_status = 503
    error_code = "SERVICE_UNAVAILABLE"
    log_level = logging.WARNING
```

- [ ] **Step 4: Run test**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_exceptions.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
cd deploy-service && git add app/core/exceptions.py tests/unit/test_exceptions.py
git commit -m "feat(exceptions): add ServiceUnavailableException (503)"
```

---

## Task 2: Promote `httpx` to a direct dependency

**Files:**
- Modify: `deploy-service/pyproject.toml`

- [ ] **Step 1: Edit `pyproject.toml`**

Add `httpx>=0.27.0` to `[project].dependencies` (it is currently only in `dev`). The block becomes:

```toml
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.29.0",
    "python-jose[cryptography]>=3.3.0",
    "bcrypt>=4.0.0",
    "pydantic-settings>=2.2.0",
    "python-multipart>=0.0.9",
    "python-gitlab>=4.0.0",
    "ansi2html>=1.9.2",
    "asyncssh>=2.14.0",
    "redis>=5.0.0",
    "prometheus-fastapi-instrumentator>=7.0.0",
    "httpx>=0.27.0",
]
```

(`httpx` remains in `[dependency-groups].dev` too — that is fine and harmless.)

- [ ] **Step 2: Sync deps**

```bash
cd deploy-service && uv sync --group dev
```
Expected: completes without error.

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add pyproject.toml uv.lock
git commit -m "chore(deps): promote httpx to a direct dependency"
```

---

## Task 3: Domain — `HostType` enum, request/state fields, poll response extras

**Files:**
- Modify: `deploy-service/app/domain/command.py`
- Test: `deploy-service/tests/unit/test_command_domain.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `deploy-service/tests/unit/test_command_domain.py`:

```python
import pytest
from pydantic import ValidationError

from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    CommandState, CommandStatus, HostType,
)


def test_host_type_enum_values():
    assert HostType.IP == "ip"
    assert HostType.BASTION == "bastion"
    assert HostType.HOSTNAME == "hostname"


def test_request_defaults_host_type_to_ip():
    req = CommandExecutionRequest(
        command_name="ls", host="10.0.0.1", username="root",
    )
    assert req.host_type == HostType.IP


def test_request_accepts_explicit_host_type():
    req = CommandExecutionRequest(
        command_name="ls", host="node-a01", username="root", host_type="hostname",
    )
    assert req.host_type == HostType.HOSTNAME


def test_request_rejects_unknown_host_type():
    with pytest.raises(ValidationError):
        CommandExecutionRequest(
            command_name="ls", host="x", username="root", host_type="dns",
        )


def test_command_state_has_resolved_ip_and_host_type():
    state = CommandState(
        command_id="abc",
        status=CommandStatus.RUNNING,
        host="node-a01",
        host_type=HostType.HOSTNAME,
        resolved_ip="10.0.1.10",
        port=22,
        username="root",
        ssh_config="default",
        request_id="rid",
        exec_command="ls",
        killable=True,
        pgids=[],
    )
    assert state.host_type == HostType.HOSTNAME
    assert state.resolved_ip == "10.0.1.10"


def test_response_defaults_for_new_metadata_fields():
    resp = CommandExecutionResponse(status="running")
    assert resp.host_type is None
    assert resp.resolved_ip is None
    assert resp.pgids == []


def test_response_accepts_new_metadata_fields():
    resp = CommandExecutionResponse(
        status="running",
        host_type=HostType.HOSTNAME,
        resolved_ip="10.0.1.10",
        pgids=[1234, 5678],
    )
    assert resp.host_type == HostType.HOSTNAME
    assert resp.resolved_ip == "10.0.1.10"
    assert resp.pgids == [1234, 5678]
```

- [ ] **Step 2: Run to verify failure**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_command_domain.py -v
```
Expected: ImportError on `HostType`.

- [ ] **Step 3: Update `app/domain/command.py`**

Near the top of the file (after the existing `from enum import Enum` import on line 4), add:

```python
class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"
```

In `CommandExecutionRequest` add `host_type` (place after `host`):

```python
class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    host_type: HostType = HostType.IP
    port: int = 22
    username: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)
```

In `CommandState` add the two new fields (after existing `host: str`):

```python
class CommandState(BaseModel):
    command_id: str
    status: CommandStatus
    output: Optional[str] = None
    exit_code: Optional[int] = None
    message: Optional[str] = None

    # execution metadata
    host: str
    host_type: HostType = HostType.IP
    resolved_ip: str
    port: int
    username: str
    ssh_config: str
    request_id: str
    exec_command: str

    # control
    killable: bool
    pgids: List[int] = Field(default_factory=list)
```

(The default `HostType.IP` keeps backwards compatibility for any state objects still in Redis from before this change — the `update_if` path will read them as `ip`.)

Also extend `CommandExecutionResponse` with three optional fields for the poll endpoint:

```python
class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None
    exec_command: Optional[str] = None
    # Populated only by GET /command/execution/{id}; surfaced from CommandState.
    host_type: Optional[HostType] = None
    resolved_ip: Optional[str] = None
    pgids: List[int] = Field(default_factory=list)
```

Leave the `success()` / `failed()` classmethods alone — they intentionally don't set these fields. The POST path builds the running-acknowledgement response directly (`CommandExecutionResponse(status=..., command_id=...)`) so it already won't carry the extras.

- [ ] **Step 4: Run tests**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_command_domain.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
cd deploy-service && git add app/domain/command.py tests/unit/test_command_domain.py
git commit -m "feat(domain): add HostType enum and host_type/resolved_ip fields"
```

---

## Task 4: Inventory repository — models, ABC, and `HttpInventoryRepository`

**Files:**
- Create: `deploy-service/app/repositories/inventory_repository.py`
- Test: `deploy-service/tests/unit/test_inventory_repository.py`

- [ ] **Step 1: Write the failing tests**

Create `deploy-service/tests/unit/test_inventory_repository.py`:

```python
import httpx
import pytest

from app.core.exceptions import (
    NotFoundException, UpstreamTimeoutException, UpstreamUnavailableException,
)
from app.repositories.inventory_repository import (
    HttpInventoryRepository, InventoryBastion, InventoryHostInfo,
)


def _client(handler) -> HttpInventoryRepository:
    transport = httpx.MockTransport(handler)
    return HttpInventoryRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


@pytest.mark.asyncio
async def test_lookup_success_returns_info():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/inventory/hosts/node-a01"
        assert request.headers.get("authorization") == "Bearer t"
        return httpx.Response(
            200,
            json={
                "hostname": "node-a01",
                "ip": "10.0.1.10",
                "bastion": {"hostname": "bastion-a", "ip": "10.0.0.5"},
            },
        )

    repo = _client(handler)
    info = await repo.lookup("node-a01")
    assert info == InventoryHostInfo(
        hostname="node-a01",
        ip="10.0.1.10",
        bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
    )


@pytest.mark.asyncio
async def test_lookup_404_raises_not_found():
    repo = _client(lambda r: httpx.Response(404, json={"detail": "nope"}))
    with pytest.raises(NotFoundException):
        await repo.lookup("missing")


@pytest.mark.asyncio
async def test_lookup_401_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(401, json={"detail": "no auth"}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup("x")


@pytest.mark.asyncio
async def test_lookup_500_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup("x")


@pytest.mark.asyncio
async def test_lookup_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    repo = _client(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.lookup("x")


@pytest.mark.asyncio
async def test_lookup_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)
    repo = _client(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup("x")
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_inventory_repository.py -v
```
Expected: ImportError.

- [ ] **Step 3: Create `app/repositories/inventory_repository.py`**

```python
"""Inventory API repository.

Defines the abstract InventoryRepository contract plus an HTTP-backed
implementation. The repository hides httpx from the service layer and
translates HTTP outcomes into the existing application exception
hierarchy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import httpx
from pydantic import BaseModel

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


class InventoryBastion(BaseModel):
    hostname: str
    ip: str


class InventoryHostInfo(BaseModel):
    hostname: str
    ip: str
    bastion: InventoryBastion


class InventoryRepository(ABC):
    """Look up an Inventory record by hostname."""

    @abstractmethod
    async def lookup(self, hostname: str) -> InventoryHostInfo: ...


class HttpInventoryRepository(InventoryRepository):
    """HTTP-backed InventoryRepository.

    GET {base_url}/inventory/hosts/{hostname}
    Header: Authorization: Bearer {token}
    """

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
        # transport is injectable for testing (httpx.MockTransport).
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def lookup(self, hostname: str) -> InventoryHostInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"/inventory/hosts/{hostname}",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"Inventory lookup for '{hostname}' timed out after {self._timeout}s.",
                detail={"hostname": hostname},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"Inventory lookup for '{hostname}' failed: {exc}",
                detail={"hostname": hostname},
            ) from exc

        if resp.status_code == 404:
            raise NotFoundException(
                f"Host '{hostname}' not found in inventory.",
                detail={"hostname": hostname},
            )
        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"Inventory returned {resp.status_code} for '{hostname}'.",
                detail={"hostname": hostname, "status_code": resp.status_code},
            )

        return InventoryHostInfo.model_validate(resp.json())
```

- [ ] **Step 4: Run tests**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_inventory_repository.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
cd deploy-service && git add app/repositories/inventory_repository.py tests/unit/test_inventory_repository.py
git commit -m "feat(inventory): add HttpInventoryRepository with exception mapping"
```

---

## Task 5: Host resolver — interface, three implementations, factory

**Files:**
- Create: `deploy-service/app/repositories/host_resolver.py`
- Create: `deploy-service/tests/fixtures/inventory.py`
- Test: `deploy-service/tests/unit/test_host_resolver.py`

- [ ] **Step 1: Create the in-memory test double**

`deploy-service/tests/fixtures/inventory.py`:

```python
"""In-memory InventoryRepository for tests."""

from typing import Dict

from app.core.exceptions import NotFoundException
from app.repositories.inventory_repository import (
    InventoryHostInfo, InventoryRepository,
)


class InMemoryInventoryRepository(InventoryRepository):
    def __init__(self, records: Dict[str, InventoryHostInfo]):
        self._records = records

    async def lookup(self, hostname: str) -> InventoryHostInfo:
        info = self._records.get(hostname)
        if info is None:
            raise NotFoundException(
                f"Host '{hostname}' not found in inventory.",
                detail={"hostname": hostname},
            )
        return info
```

- [ ] **Step 2: Write the failing tests**

`deploy-service/tests/unit/test_host_resolver.py`:

```python
import pytest

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.host_resolver import (
    BastionHostResolver, HostnameHostResolver, IpHostResolver,
    ResolvedHost, create_host_resolver,
)
from app.repositories.inventory_repository import (
    InventoryBastion, InventoryHostInfo,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _inventory():
    return InMemoryInventoryRepository({
        "node-a01": InventoryHostInfo(
            hostname="node-a01", ip="10.0.1.10",
            bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
        ),
    })


@pytest.mark.asyncio
async def test_ip_resolver_returns_input_unchanged():
    resolver = IpHostResolver()
    resolved = await resolver.resolve("10.0.0.1")
    assert resolved == ResolvedHost(ip="10.0.0.1", source_input="10.0.0.1", metadata={})


@pytest.mark.asyncio
async def test_hostname_resolver_returns_host_ip():
    resolver = HostnameHostResolver(_inventory())
    resolved = await resolver.resolve("node-a01")
    assert resolved.ip == "10.0.1.10"
    assert resolved.source_input == "node-a01"
    assert resolved.metadata == {"hostname": "node-a01"}


@pytest.mark.asyncio
async def test_bastion_resolver_returns_bastion_ip():
    resolver = BastionHostResolver(_inventory())
    resolved = await resolver.resolve("node-a01")
    assert resolved.ip == "10.0.0.5"
    assert resolved.source_input == "node-a01"
    assert resolved.metadata == {
        "hostname": "node-a01",
        "bastion_hostname": "bastion-a",
    }


@pytest.mark.asyncio
async def test_hostname_resolver_propagates_not_found():
    resolver = HostnameHostResolver(_inventory())
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing")


def test_factory_returns_correct_resolver_class():
    inv = _inventory()
    assert isinstance(create_host_resolver(HostType.IP, inv), IpHostResolver)
    assert isinstance(create_host_resolver(HostType.HOSTNAME, inv), HostnameHostResolver)
    assert isinstance(create_host_resolver(HostType.BASTION, inv), BastionHostResolver)
```

- [ ] **Step 3: Run to verify failure**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_host_resolver.py -v
```
Expected: ImportError.

- [ ] **Step 4: Create `app/repositories/host_resolver.py`**

```python
"""Host resolver strategy: chooses the SSH target IP based on host_type.

Adding a new host type:
  1. Add a value to HostType in app/domain/command.py.
  2. Add a HostResolver subclass here.
  3. Add a branch to create_host_resolver().
CommandService does not need to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

from pydantic import BaseModel, Field

from app.domain.command import HostType
from app.repositories.inventory_repository import InventoryRepository


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


class BastionHostResolver(HostResolver):
    def __init__(self, inventory: InventoryRepository) -> None:
        self._inventory = inventory

    async def resolve(self, raw_host: str) -> ResolvedHost:
        info = await self._inventory.lookup(raw_host)
        return ResolvedHost(
            ip=info.bastion.ip,
            source_input=raw_host,
            metadata={
                "hostname": info.hostname,
                "bastion_hostname": info.bastion.hostname,
            },
        )


def create_host_resolver(
    host_type: HostType, inventory: InventoryRepository,
) -> HostResolver:
    if host_type == HostType.IP:
        return IpHostResolver()
    if host_type == HostType.HOSTNAME:
        return HostnameHostResolver(inventory)
    if host_type == HostType.BASTION:
        return BastionHostResolver(inventory)
    raise ValueError(f"Unsupported host_type: {host_type}")
```

- [ ] **Step 5: Run tests**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_host_resolver.py -v
```
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
cd deploy-service && git add app/repositories/host_resolver.py tests/unit/test_host_resolver.py tests/fixtures/inventory.py
git commit -m "feat(resolver): add HostResolver strategy with ip/hostname/bastion implementations"
```

---

## Task 6: Settings — Inventory config

**Files:**
- Modify: `deploy-service/app/core/config.py`
- Modify: `deploy-service/.env.dev`
- Modify: `deploy-service/.env.test`

- [ ] **Step 1: Add Settings fields**

In `deploy-service/app/core/config.py`, after the SSH Command API block (around line 61, after `SSH_CONNECT_TIMEOUT_SECONDS`), insert:

```python
    # ── Inventory API ─────────────────────────────────────────────────────────
    INVENTORY_API_URL: str = "http://localhost:9001"
    INVENTORY_API_TOKEN: str = "fake-inventory-token"
    INVENTORY_API_TIMEOUT_SECONDS: float = 5.0
```

- [ ] **Step 2: Update `.env.dev`**

Append to `deploy-service/.env.dev`:

```
# Inventory API (fake one runs locally on :9001 via `make inventory-api`)
INVENTORY_API_URL=http://localhost:9001
INVENTORY_API_TOKEN=fake-inventory-token
INVENTORY_API_TIMEOUT_SECONDS=5
```

- [ ] **Step 3: Update `.env.test`**

Append to `deploy-service/.env.test`:

```
# Inventory API — tests override via dependency injection, but config must load
INVENTORY_API_URL=http://localhost:9001
INVENTORY_API_TOKEN=test-token
INVENTORY_API_TIMEOUT_SECONDS=1
```

- [ ] **Step 4: Verify settings load**

```bash
cd deploy-service && APP_ENV=test uv run python -c "from app.core.config import get_settings; s = get_settings(); print(s.INVENTORY_API_URL, s.INVENTORY_API_TOKEN, s.INVENTORY_API_TIMEOUT_SECONDS)"
```
Expected: `http://localhost:9001 test-token 1.0`.

- [ ] **Step 5: Commit**

```bash
cd deploy-service && git add app/core/config.py .env.dev .env.test
git commit -m "feat(config): add INVENTORY_API_* settings"
```

---

## Task 7: DI factories — `get_inventory_repository`, update `get_command_service`

**Files:**
- Modify: `deploy-service/app/core/dependencies.py`

(Behaviour is exercised by Task 8 / integration tests. No standalone test added — `get_inventory_repository` is a thin factory.)

- [ ] **Step 1: Edit `app/core/dependencies.py`**

Append after `get_command_state_repository`:

```python
from app.core.config import get_settings
from app.repositories.inventory_repository import (
    HttpInventoryRepository, InventoryRepository,
)


async def get_inventory_repository() -> InventoryRepository:
    s = get_settings()
    return HttpInventoryRepository(
        base_url=s.INVENTORY_API_URL,
        token=s.INVENTORY_API_TOKEN,
        timeout_seconds=s.INVENTORY_API_TIMEOUT_SECONDS,
    )
```

And update `get_command_service` to inject inventory:

```python
async def get_command_service(
    repo: CommandStateRepository = Depends(get_command_state_repository),
    inventory: InventoryRepository = Depends(get_inventory_repository),
) -> CommandService:
    return CommandService(repo, inventory)
```

- [ ] **Step 2: Smoke-import**

```bash
cd deploy-service && APP_ENV=test uv run python -c "from app.core.dependencies import get_command_service, get_inventory_repository; print('ok')"
```
Expected: `ok`. (Will print `ok` only after Task 8 makes `CommandService` accept the new param. For now this step will fail — that's fine; the next task fixes it.)

> Order note: combine Task 7 and Task 8 into one commit if you prefer; the smoke step is just informational.

- [ ] **Step 3: Stage (do not commit yet — combined with Task 8)**

```bash
cd deploy-service && git add app/core/dependencies.py
```

---

## Task 8: CommandService — accept inventory, async prepare, use resolver, persist resolved state, raise typed exceptions

**Files:**
- Modify: `deploy-service/app/services/command_service.py`

This is the largest single edit. Read the steps fully before starting.

- [ ] **Step 1: Update imports and constructor**

In `deploy-service/app/services/command_service.py`:

Replace the imports block (around lines 12–26) with:

```python
from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    UserCommandWhitelist, CommandWhitelistConfig,
    SSHConnectionConfig, RunningCommandEntry, ExecutionContext,
    CommandState, CommandStatus, HostType,
)
from app.core.config import get_settings
from app.core.redis_client import RedisClient
from app.repositories.ssh_auth_repository import create_authenticator
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.host_resolver import ResolvedHost, create_host_resolver
from app.core.exceptions import (
    CommandExecutionException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
    ForbiddenException,
    NotFoundException,
    ConflictException,
    ServiceUnavailableException,
    BaseAppException,
)
```

Update the constructor:

```python
class CommandService:
    def __init__(self, repo: CommandStateRepository, inventory: InventoryRepository):
        self.repo = repo
        self.inventory = inventory
```

- [ ] **Step 2: Add `resolved_host` to `ExecutionContext`**

Open `deploy-service/app/domain/command.py` and update `ExecutionContext` (the very bottom):

```python
@dataclass
class ExecutionContext:
    username: str
    request_id: str
    command_name: str
    raw_request: CommandExecutionRequest
    cmd_config: CommandWhitelistConfig
    ssh_config: SSHConnectionConfig
    resolved_host: "ResolvedHost"     # NEW; quoted to avoid circular import
    conn: Optional[asyncssh.SSHClientConnection] = None
    pipeline_cmds: List[List[str]] = field(default_factory=list)
```

(Use a forward reference; `ResolvedHost` lives in `app.repositories.host_resolver`. If you prefer not to use a string annotation, do `from typing import TYPE_CHECKING` then import inside that guard.)

- [ ] **Step 3: Rewrite `_load_user_whitelist` to raise Forbidden on missing file**

Replace the existing method body:

```python
def _load_user_whitelist(self, username: str) -> UserCommandWhitelist:
    file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"allow-commands-{username}.json")
    if not os.path.exists(file_path):
        raise ForbiddenException(
            f"User '{username}' has no command whitelist configured.",
            detail={"username": username},
        )
    with open(file_path, "r") as f:
        data = json.load(f)
    return UserCommandWhitelist(**data)
```

- [ ] **Step 4: Rewrite `_load_ssh_config` to raise on missing file**

```python
def _load_ssh_config(self, target: str) -> SSHConnectionConfig:
    file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"SSH-{target}.json")
    if not os.path.exists(file_path):
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, "SSH-default.json")
        if not os.path.exists(file_path):
            raise BaseAppException(
                "SSH configuration not found.",
                detail={"target": target},
            )
    with open(file_path, "r") as f:
        data = json.load(f)
    return SSHConnectionConfig(**data)
```

- [ ] **Step 5: Rewrite `_prepare_execution` (now async, uses resolver, raises typed exceptions)**

Replace the entire `_prepare_execution` method with:

```python
async def _prepare_execution(
    self, username: str, request_id: str, req: CommandExecutionRequest,
) -> ExecutionContext:
    whitelist = self._load_user_whitelist(username)

    resolver = create_host_resolver(req.host_type, self.inventory)
    resolved = await resolver.resolve(req.host)

    if any(re.match(pattern, resolved.ip) for pattern in whitelist.deny_hosts):
        logger.warning(
            f"Host '{resolved.ip}' is blocked for user '{username}' by deny list.",
            extra={
                "request_id": request_id, "username": username,
                "host": req.host, "host_type": req.host_type.value,
                "resolved_ip": resolved.ip,
            },
        )
        raise ForbiddenException(
            f"Host '{resolved.ip}' is blocked.",
            detail={"host": req.host, "resolved_ip": resolved.ip},
        )

    if not any(re.match(pattern, resolved.ip) for pattern in whitelist.allow_hosts):
        logger.warning(
            f"Host '{resolved.ip}' is not allowed for user '{username}' by allow list.",
            extra={
                "request_id": request_id, "username": username,
                "host": req.host, "host_type": req.host_type.value,
                "resolved_ip": resolved.ip,
            },
        )
        raise ForbiddenException(
            f"Host '{resolved.ip}' is not allowed.",
            detail={"host": req.host, "resolved_ip": resolved.ip},
        )

    cmd_config = next(
        (c for c in whitelist.allow_commands if c.command_name == req.command_name),
        None,
    )
    if not cmd_config:
        raise ForbiddenException(
            f"Command '{req.command_name}' not in user '{username}' whitelist.",
            detail={"command_name": req.command_name, "username": username},
        )

    for arg_conf in cmd_config.arguments:
        val = req.arguments.get(arg_conf.name)
        if val is None:
            raise CommandExecutionException(
                f"Missing required argument: {arg_conf.name}",
                detail={"argument": arg_conf.name},
            )
        val_str = str(val)
        self._validate_anti_injection(val_str)
        if arg_conf.validation_regex:
            if not re.match(arg_conf.validation_regex, val_str):
                raise CommandExecutionException(
                    f"Argument '{arg_conf.name}' does not match validation regex.",
                    detail={"argument": arg_conf.name},
                )

    ssh_config = self._load_ssh_config(req.ssh_config)

    return ExecutionContext(
        username=username,
        request_id=request_id,
        command_name=req.command_name,
        raw_request=req,
        cmd_config=cmd_config,
        ssh_config=ssh_config,
        resolved_host=resolved,
    )
```

- [ ] **Step 6: Make `_check_capacity` raise instead of returning a failed response**

```python
def _check_capacity(self, username: str, request_id: str) -> None:
    if len(_local_running_commands) >= settings.COMMAND_MAX_RUNNING:
        logger.warning(
            f"Max running commands reached ({settings.COMMAND_MAX_RUNNING}), rejecting new request.",
            extra={"request_id": request_id, "username": username},
        )
        raise ServiceUnavailableException(
            f"Too many running commands (limit: {settings.COMMAND_MAX_RUNNING}). "
            "Please try again later.",
            detail={"max_running": settings.COMMAND_MAX_RUNNING},
        )
```

- [ ] **Step 7: Update `_connect` to use resolved IP**

In `_connect`, replace the body's host references:

```python
async def _connect(self, context: ExecutionContext, req: CommandExecutionRequest) -> asyncssh.SSHClientConnection:
    authenticator = create_authenticator(context.ssh_config)
    conn_kwargs = authenticator.get_connect_kwargs()
    ip = context.resolved_host.ip
    target = f"{ip}:{req.port}"
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host=ip,
                port=req.port,
                username=req.username,
                **conn_kwargs,
            ),
            timeout=settings.SSH_CONNECT_TIMEOUT_SECONDS,
        )
        return conn
    except asyncio.TimeoutError as exc:
        raise UpstreamTimeoutException(
            f"SSH connection to {target} (host_type={req.host_type.value}, raw={req.host}) "
            f"timed out after {settings.SSH_CONNECT_TIMEOUT_SECONDS}s.",
            detail={
                "host": req.host, "host_type": req.host_type.value,
                "resolved_ip": ip, "port": req.port,
            },
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        raise UpstreamUnavailableException(
            f"SSH connection to {target} (host_type={req.host_type.value}, raw={req.host}) failed: {exc}",
            detail={
                "host": req.host, "host_type": req.host_type.value,
                "resolved_ip": ip, "port": req.port,
            },
        ) from exc
```

- [ ] **Step 8: Persist resolved fields in `CommandState`**

In `_handle_async_execution`, update the `CommandState(...)` construction:

```python
state = CommandState(
    command_id=command_id,
    status=CommandStatus.RUNNING,
    host=context.raw_request.host,
    host_type=context.raw_request.host_type,
    resolved_ip=context.resolved_host.ip,
    port=context.raw_request.port,
    username=context.username,
    ssh_config=context.raw_request.ssh_config,
    request_id=context.request_id,
    killable=context.cmd_config.killable,
    pgids=[],
    exec_command=cmd_str_preview,
)
```

Also update the `RunningCommandEntry`:

```python
entry = RunningCommandEntry(
    host_ip=context.resolved_host.ip,
    killable=context.cmd_config.killable,
    conn=context.conn,
)
```

- [ ] **Step 9: Cross-pod kill uses `resolved_ip`**

In `kill_command`, replace the cross-pod SSH connect block (around the line `conn = await asyncio.wait_for(asyncssh.connect(host=state.host, ...)`):

```python
conn = await asyncio.wait_for(
    asyncssh.connect(
        host=state.resolved_ip,
        port=state.port,
        username=state.username,
        **conn_kwargs,
    ),
    timeout=10,
)
```

- [ ] **Step 10: Rewrite `execute_command` to propagate exceptions**

```python
async def execute_command(
    self, username: str, request_id: str, req: CommandExecutionRequest,
) -> CommandExecutionResponse:
    self._check_capacity(username, request_id)

    context = await self._prepare_execution(username, request_id, req)
    context.pipeline_cmds = self._build_pipeline(context)

    conn = await self._connect(context, req)
    context.conn = conn

    if context.cmd_config.disconnects_ssh:
        return await self._handle_fire_and_forget(context)

    return await self._handle_async_execution(context)
```

- [ ] **Step 11: Run the existing unit test (which uses `CommandService(None)`)**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_command_service.py -v
```

Expected: tests still pass even though the constructor signature changed — the call sites pass `None` positionally for `repo`, but the new `inventory` arg is positional too. **Update the existing tests** to pass `None` twice:

In `deploy-service/tests/unit/test_command_service.py`, change `CommandService(None)` to `CommandService(None, None)`. Run again, expect green.

- [ ] **Step 12: Stage (commit combined with Task 7 + the test fix)**

```bash
cd deploy-service && git add app/services/command_service.py app/domain/command.py app/core/dependencies.py tests/unit/test_command_service.py
git commit -m "feat(command): inject inventory, use resolver, propagate typed exceptions"
```

---

## Task 9: Router cleanup — drop redundant `try/except`, kill returns 409

**Files:**
- Modify: `deploy-service/app/api/v1/command.py`

- [ ] **Step 1: Edit the file**

Replace the existing file content with the cleaned-up version (preserving structure):

```python
import logging
from fastapi import APIRouter, Depends, Request

from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    UserCommandWhitelist, CommandWhitelistConfig,
)
from app.services.command_service import CommandService
from app.core.dependencies import get_current_user, get_command_service
from app.core.exceptions import (
    ConflictException, NotFoundException,
)
from app.domain.models import User, ApiResponse

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/command", tags=["command"])


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@router.get(
    "/info",
    response_model=ApiResponse[UserCommandWhitelist],
    summary="Get All Available Commands",
)
async def get_all_commands_info(
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[UserCommandWhitelist]:
    whitelist = svc.get_user_commands(current_user.account)
    return ApiResponse(data=whitelist, request_id=_request_id(request))


@router.get(
    "/{command_name}/info",
    response_model=ApiResponse[CommandWhitelistConfig],
    summary="Get Specific Command Info",
)
async def get_specific_command_info(
    command_name: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandWhitelistConfig]:
    cmd_info = svc.get_command_info(current_user.account, command_name)
    return ApiResponse(data=cmd_info, request_id=_request_id(request))


@router.post(
    "/execution",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Execute SSH Command Pipeline",
)
async def execute_command_endpoint(
    request: Request,
    body: CommandExecutionRequest,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandExecutionResponse]:
    req_id = _request_id(request)
    response_data = await svc.execute_command(current_user.account, req_id, body)
    return ApiResponse(data=response_data, request_id=req_id)


@router.get(
    "/execution/{command_id}",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Poll Command Execution Result",
)
async def get_command_execution_status(
    command_id: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandExecutionResponse]:
    response_data = await svc.get_command_execution_result(command_id)
    return ApiResponse(data=response_data, request_id=_request_id(request))


@router.post(
    "/execution/{command_id}/kill",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Kill Running Command",
)
async def kill_command_endpoint(
    command_id: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandExecutionResponse]:
    req_id = _request_id(request)
    try:
        state = await svc.repo.get(command_id)
    except Exception as exc:
        raise NotFoundException(
            f"Command {command_id} not found.", detail={"command_id": command_id}
        ) from exc

    if state.status != "running":
        raise ConflictException(
            f"Cannot kill command in {state.status} state.",
            detail={"command_id": command_id, "current_status": state.status},
        )

    await svc.kill_command(command_id, message="Killed by user request.")

    return ApiResponse(
        data=CommandExecutionResponse(
            command_id=command_id,
            status="accepted",
            message="Kill request accepted",
        ),
        request_id=req_id,
    )
```

- [ ] **Step 2: Update `get_command_execution_result` to raise `NotFoundException` and surface metadata**

In `deploy-service/app/services/command_service.py`, update `get_command_execution_result` so it raises 404 on miss and copies the new metadata fields out of `CommandState`:

```python
async def get_command_execution_result(self, command_id: str) -> CommandExecutionResponse:
    try:
        state = await self.repo.get(command_id)
    except CommandExecutionException as exc:
        raise NotFoundException(
            f"Command {command_id} not found.",
            detail={"command_id": command_id},
        ) from exc
    return CommandExecutionResponse(
        status=state.status,
        command_id=state.command_id,
        exit_status=state.exit_code,
        output=state.output,
        message=state.message or "",
        exec_command=state.exec_command,
        host_type=state.host_type,
        resolved_ip=state.resolved_ip,
        pgids=state.pgids,
    )
```

(`get_user_commands` / `get_command_info` already raise `CommandExecutionException` which is HTTP 400 — fine for whitelist-missing or command-not-in-whitelist.)

- [ ] **Step 2b: Add integration assertion for the new poll fields**

In `deploy-service/tests/integration/test_command_host_type.py` (created in Task 12), append a test that uses dependency overrides to plant a fully-formed `CommandState` and verify it round-trips through GET. Since the previous tests in Task 12 stop execution early via a `RuntimeError` inside `create_process`, this test is a smaller separate scenario — paste the helper directly:

```python
def test_poll_response_surfaces_host_type_resolved_ip_and_pgids(
    client_with_inventory, monkeypatch,
):
    """GET /command/execution/{id} should expose host_type, resolved_ip, pgids."""
    from app.core.dependencies import get_command_state_repository
    from app.domain.command import CommandState, CommandStatus, HostType

    fixed_state = CommandState(
        command_id="fixed-id",
        status=CommandStatus.SUCCESS,
        host="node-a01",
        host_type=HostType.HOSTNAME,
        resolved_ip="10.0.1.10",
        port=22,
        username="root",
        ssh_config="default",
        request_id="rid",
        exec_command="ls",
        killable=True,
        pgids=[111, 222],
        exit_code=0,
        output="ok",
    )

    class _StubRepo:
        async def get(self, command_id):
            assert command_id == "fixed-id"
            return fixed_state

    client_with_inventory.app.dependency_overrides[
        get_command_state_repository
    ] = lambda: _StubRepo()

    token = _get_token(client_with_inventory)
    resp = client_with_inventory.get(
        "/api/v1/command/execution/fixed-id",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["host_type"] == "hostname"
    assert data["resolved_ip"] == "10.0.1.10"
    assert data["pgids"] == [111, 222]
    assert data["exit_status"] == 0
    assert data["output"] == "ok"
```

The test count in Task 12's final step rises from 5 to 6.

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add app/api/v1/command.py app/services/command_service.py
git commit -m "refactor(api): let exceptions propagate, kill returns 409 on bad state"
```

---

## Task 10: Service-level error unit tests

**Files:**
- Create: `deploy-service/tests/unit/test_command_service_errors.py`

- [ ] **Step 1: Write the tests**

```python
"""Verify CommandService raises typed exceptions for input/policy errors
instead of returning 200 + failed body.
"""

import os
import json
from pathlib import Path
import pytest

from app.core.exceptions import (
    CommandExecutionException, ForbiddenException, ServiceUnavailableException,
)
from app.domain.command import CommandExecutionRequest, HostType
from app.services.command_service import CommandService
import app.services.command_service as cs_mod
from tests.fixtures.inventory import InMemoryInventoryRepository
from app.repositories.inventory_repository import (
    InventoryBastion, InventoryHostInfo,
)


def _whitelist_file(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "allow-commands-test_admin.json"
    p.write_text(json.dumps(body))
    return p


def _ssh_default(tmp_path: Path) -> Path:
    p = tmp_path / "SSH-default.json"
    p.write_text(json.dumps({"auth_method": "key", "key_base64": "AA=="}))
    return p


@pytest.fixture
def svc(tmp_path, monkeypatch):
    # Point COMMAND_CONFIG_DIR at tmp_path so file lookups isolate.
    from app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("COMMAND_CONFIG_DIR", str(tmp_path))
    # Reset settings to pick the new env var
    get_settings.cache_clear()
    # Patch the module-level `settings` cached in command_service
    monkeypatch.setattr(cs_mod, "settings", get_settings())

    inventory = InMemoryInventoryRepository({
        "node-a01": InventoryHostInfo(
            hostname="node-a01", ip="10.0.1.10",
            bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
        ),
    })
    return CommandService(repo=None, inventory=inventory), tmp_path


@pytest.mark.asyncio
async def test_no_whitelist_file_raises_forbidden(svc):
    service, _ = svc
    req = CommandExecutionRequest(
        command_name="ls", host="10.0.0.1", username="root",
    )
    with pytest.raises(ForbiddenException):
        await service._prepare_execution("test_admin", "rid", req)


@pytest.mark.asyncio
async def test_deny_host_raises_forbidden(svc):
    service, tmp_path = svc
    _whitelist_file(tmp_path, {
        "name": "admin", "allow_hosts": [".*"], "deny_hosts": ["10\\.0\\.1\\.10"],
        "allow_commands": [{
            "command_name": "ls", "pipeline": [{"command": ["ls"]}],
            "arguments": [],
        }],
    })
    _ssh_default(tmp_path)
    req = CommandExecutionRequest(
        command_name="ls", host="node-a01", username="root", host_type="hostname",
    )
    with pytest.raises(ForbiddenException):
        await service._prepare_execution("test_admin", "rid", req)


@pytest.mark.asyncio
async def test_command_not_in_whitelist_raises_forbidden(svc):
    service, tmp_path = svc
    _whitelist_file(tmp_path, {
        "name": "admin", "allow_hosts": [".*"], "deny_hosts": [],
        "allow_commands": [{
            "command_name": "ls", "pipeline": [{"command": ["ls"]}],
            "arguments": [],
        }],
    })
    _ssh_default(tmp_path)
    req = CommandExecutionRequest(
        command_name="reboot", host="10.0.0.1", username="root",
    )
    with pytest.raises(ForbiddenException):
        await service._prepare_execution("test_admin", "rid", req)


@pytest.mark.asyncio
async def test_missing_argument_raises_command_execution_exception(svc):
    service, tmp_path = svc
    _whitelist_file(tmp_path, {
        "name": "admin", "allow_hosts": [".*"], "deny_hosts": [],
        "allow_commands": [{
            "command_name": "sleep", "pipeline": [{"command": ["sleep", "{time}"]}],
            "arguments": [{"name": "time", "type": "int", "validation_regex": "^\\d+$"}],
        }],
    })
    _ssh_default(tmp_path)
    req = CommandExecutionRequest(
        command_name="sleep", host="10.0.0.1", username="root", arguments={},
    )
    with pytest.raises(CommandExecutionException):
        await service._prepare_execution("test_admin", "rid", req)


@pytest.mark.asyncio
async def test_capacity_full_raises_service_unavailable(svc, monkeypatch):
    service, _ = svc
    # Fill the running-commands pool to the configured limit.
    monkeypatch.setattr(cs_mod.settings, "COMMAND_MAX_RUNNING", 1)
    monkeypatch.setattr(cs_mod, "_local_running_commands", {"x": object()})
    with pytest.raises(ServiceUnavailableException):
        service._check_capacity("test_admin", "rid")
```

- [ ] **Step 2: Run tests**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_command_service_errors.py -v
```
Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add tests/unit/test_command_service_errors.py
git commit -m "test(command): cover typed-exception paths in _prepare_execution"
```

---

## Task 11: Update existing integration test expectations

**Files:**
- Modify: `deploy-service/tests/integration/test_ssh_command_api.py`

- [ ] **Step 1: Update `test_execute_invalid_argument_regex`**

Replace its body so it asserts 400 instead of 200 + failed body:

```python
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
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "COMMAND_EXECUTION_ERROR"
    assert "validation regex" in body["error"]["message"]
```

- [ ] **Step 2: Run tests**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_ssh_command_api.py -v
```

Two of these tests need real SSH containers (`make setup-ssh-nodes`); if you do not have them running, only `test_execute_invalid_argument_regex` is meaningful here — confirm it passes. The other two cases may be skipped or marked-failed locally; the goal of this task is **not** to fix every integration test, only to update assertions for the cases this plan changes.

> If `test_execute_reboot_fire_and_forget` or `test_execute_list_file` currently rely on `cluster1` SSH config being present and the test SSH nodes running, leave them as-is. They are out of scope for this change.

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add tests/integration/test_ssh_command_api.py
git commit -m "test(integration): assert 400 on invalid argument regex"
```

---

## Task 12: Integration tests for host_type behaviour

**Files:**
- Create: `deploy-service/tests/integration/test_command_host_type.py`

These tests use FastAPI's dependency override system to inject an in-memory inventory **and** mock asyncssh so the test does not require real SSH targets. We only need to verify that the SSH layer was asked to connect to the **resolved IP**.

- [ ] **Step 1: Write the test file**

```python
"""Integration tests for the host_type field on /api/v1/command/execution.

We override:
  - get_inventory_repository → InMemoryInventoryRepository
  - asyncssh.connect → stub that records target host and returns a fake conn
so we can assert the final SSH target without standing up real nodes.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.core.dependencies import get_inventory_repository
from app.main import create_app
from app.repositories.inventory_repository import (
    InventoryBastion, InventoryHostInfo, InventoryRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


@pytest.fixture
def inventory() -> InventoryRepository:
    return InMemoryInventoryRepository({
        "node-a01": InventoryHostInfo(
            hostname="node-a01", ip="10.0.1.10",
            bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
        ),
    })


@pytest.fixture
def client_with_inventory(inventory):
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: inventory
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _patch_asyncssh():
    """Patch asyncssh.connect so command_service._connect records the host
    and returns a stub connection that immediately closes."""
    fake_conn = MagicMock()
    fake_conn.is_closed.return_value = True
    fake_conn.run = AsyncMock(return_value=MagicMock(
        stdout="", stderr="", exit_status=0,
    ))
    fake_conn.close = MagicMock()
    # Async create_process used in _execute_pipeline raises so the async path
    # fails fast — we only want to assert which IP _connect went to.
    fake_conn.create_process = AsyncMock(side_effect=RuntimeError("stop here"))
    return patch("app.services.command_service.asyncssh.connect",
                 new=AsyncMock(return_value=fake_conn)), fake_conn


def test_host_type_ip_connects_to_raw_ip(client_with_inventory):
    p, fake_conn = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_inventory)
        resp = client_with_inventory.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "10.0.99.99",
                "host_type": "ip",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        # Response is 200 because we accept the job; failure happens inside
        # the async task and is irrelevant — we only care about target.
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.0.99.99"


def test_host_type_hostname_connects_to_resolved_ip(client_with_inventory):
    p, fake_conn = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_inventory)
        resp = client_with_inventory.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "node-a01",
                "host_type": "hostname",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.0.1.10"


def test_host_type_bastion_connects_to_bastion_ip(client_with_inventory):
    p, fake_conn = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_inventory)
        resp = client_with_inventory.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "node-a01",
                "host_type": "bastion",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.0.0.5"


def test_hostname_not_in_inventory_returns_404(client_with_inventory):
    token = _get_token(client_with_inventory)
    resp = client_with_inventory.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "list_file",
            "host": "missing-node",
            "host_type": "hostname",
            "port": 22,
            "username": "root",
            "arguments": {"key_word": "ssh"},
        },
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"


def test_unknown_host_type_returns_422(client_with_inventory):
    token = _get_token(client_with_inventory)
    resp = client_with_inventory.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "list_file",
            "host": "node-a01",
            "host_type": "dns",
            "port": 22,
            "username": "root",
            "arguments": {"key_word": "ssh"},
        },
    )
    assert resp.status_code == 422, resp.text
```

> Note: these tests depend on `tests/fixtures/users.json` having a `test_admin` user with `command_api` scope and an `allow-commands-test_admin.json` whitelist with `list_file` allowed for all hosts (`.*`). If the fixture is missing entries, add them as part of this task — read `data/users.json` and `data/allow-commands-*.json` as references, then drop equivalents under `tests/fixtures/`. (Skip this sub-step if the fixtures already cover `list_file`.)

- [ ] **Step 2: Verify fixtures exist**

```bash
cd deploy-service && cat tests/fixtures/users.json | head -40
ls tests/fixtures/ | grep allow-commands
```
If `tests/fixtures/allow-commands-test_admin.json` is missing, copy `data/allow-commands-test_admin.json` to `tests/fixtures/`. Make sure `allow_hosts` includes `.*` and `list_file` is present.

- [ ] **Step 3: Run the tests**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_command_host_type.py -v
```
Expected: 5 passed. (A 6th test, `test_poll_response_surfaces_host_type_resolved_ip_and_pgids`, is appended later by Task 9 Step 2b — at that point this command should report 6 passed.)

- [ ] **Step 4: Commit**

```bash
cd deploy-service && git add tests/integration/test_command_host_type.py tests/fixtures/
git commit -m "test(integration): cover host_type=ip/hostname/bastion routing"
```

---

## Task 13: Fake Inventory API

**Files:**
- Create: `deploy-service/fake-api/__init__.py`
- Create: `deploy-service/fake-api/main.py`
- Create: `deploy-service/fake-api/data/inventory.json`
- Modify: `deploy-service/Makefile`

- [ ] **Step 1: Create the package marker**

```bash
mkdir -p deploy-service/fake-api/data
```

`deploy-service/fake-api/__init__.py`: empty file.

- [ ] **Step 2: Create `fake-api/main.py`**

```python
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
```

- [ ] **Step 3: Create the sample data file**

`deploy-service/fake-api/data/inventory.json`:

```json
[
  {
    "hostname": "node-a01",
    "ip": "10.0.1.10",
    "bastion": {
      "hostname": "bastion-a",
      "ip": "10.0.0.5"
    }
  },
  {
    "hostname": "node-a02",
    "ip": "10.0.1.11",
    "bastion": {
      "hostname": "bastion-a",
      "ip": "10.0.0.5"
    }
  },
  {
    "hostname": "node-b01",
    "ip": "10.0.2.10",
    "bastion": {
      "hostname": "bastion-b",
      "ip": "10.0.0.6"
    }
  }
]
```

- [ ] **Step 4: Add Makefile target**

In `deploy-service/Makefile`, after the `prod:` target, add:

```makefile
# inventory-api: 啟動本機假 Inventory API (port 9001)
.PHONY: inventory-api
inventory-api:
	APP_ENV=dev $(UV) run uvicorn fake-api.main:app --reload --port 9001
```

Also add a help line so `make help` discovers it. Inside the `help:` recipe, after the `make prod` echo line, add:

```makefile
	@echo "  make inventory-api 啟動本機假 Inventory API（port 9001）"
```

- [ ] **Step 5: Smoke-test the fake API**

In one terminal:
```bash
cd deploy-service && make inventory-api
```

In another:
```bash
curl -s -H "Authorization: Bearer anything" http://localhost:9001/inventory/hosts/node-a01
curl -s -i http://localhost:9001/inventory/hosts/node-a01   # missing auth → 401
curl -s -i -H "Authorization: Bearer anything" http://localhost:9001/inventory/hosts/nope   # 404
```

Stop the fake API (`Ctrl+C`).

- [ ] **Step 6: Commit**

```bash
cd deploy-service && git add fake-api/ Makefile
git commit -m "feat(fake-api): add standalone Inventory mock + make inventory-api target"
```

---

## Task 14: Documentation

**Files:**
- Modify: `deploy-service/ssh-command.md`

- [ ] **Step 1: Add migration notes and host-resolution section**

Read the existing `ssh-command.md` first to find the right insertion points, then add:

1. At the top of the doc, a "Migration notes (2026-05-18)" callout that lists every changed HTTP code (mirror the table from §6 of the spec).
2. A new "Host resolution" section after the request-schema description, covering:
   - The new `host_type` field, default value, and three options.
   - A short paragraph on the resolver factory pattern (one paragraph + a 4-line bullet on adding a new type).
   - A note that allow/deny lists always match the **resolved IP**.
3. Update the existing "Errors" table (or create one if absent) to the version in spec §6.
4. A "Local Inventory mock" subsection pointing at `fake-api/` and `make inventory-api`.

Keep the prose tight — one paragraph per topic. Do not re-write existing content; only add/adjust the sections above.

- [ ] **Step 2: Commit**

```bash
cd deploy-service && git add ssh-command.md
git commit -m "docs(command): document host_type, error-handling changes, fake inventory"
```

---

## Task 15: End-to-end smoke

**Files:** none (manual verification).

- [ ] **Step 1: Start dependencies**

Three terminals:

```bash
# T1
cd deploy-service && make redis-up
cd deploy-service && make inventory-api   # T2
cd deploy-service && make dev             # T3
```

- [ ] **Step 2: Get a token**

```bash
TOKEN=$(curl -s -X POST http://localhost:8001/token \
  -d "username=admin&password=<your-admin-password>" \
  | jq -r .access_token)
```

- [ ] **Step 3: Verify host_type=hostname round-trips**

```bash
curl -s -X POST http://localhost:8001/api/v1/command/execution \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "command_name": "list_file",
    "host": "node-a01",
    "host_type": "hostname",
    "port": 22,
    "username": "root",
    "arguments": {"key_word": "ssh"}
  }' | jq .
```

Expected: server logs show `SSH connection to 10.0.1.10:22 (host_type=hostname, raw=node-a01)`. The response will be a 502/504 (real SSH target isn't reachable) — that is OK; we are verifying the resolver wired through correctly.

- [ ] **Step 4: Verify unknown hostname → 404**

```bash
curl -s -i -X POST http://localhost:8001/api/v1/command/execution \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "command_name": "list_file",
    "host": "missing-host",
    "host_type": "hostname",
    "port": 22,
    "username": "root",
    "arguments": {"key_word": "ssh"}
  }'
```

Expected: `HTTP/1.1 404 Not Found`, body `{"error": {"code": "NOT_FOUND", ...}, ...}`.

- [ ] **Step 5: Verify host_type=ip is unchanged**

A normal POST without `host_type` should behave exactly as before this change.

- [ ] **Step 6: Run the full test suite**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/ -v
```
Expected: full suite green (modulo any tests that require real SSH containers — those were out of scope).

- [ ] **Step 7: No-op commit step — just confirm clean tree**

```bash
cd deploy-service && git status
```
Expected: `nothing to commit, working tree clean`.

---

## Self-review checklist (run before handoff)

- [ ] Spec §4.2 (HostResolver) → Task 5 ✔
- [ ] Spec §4.3 (Inventory repository) → Task 4 ✔
- [ ] Spec §4.4 (HostType enum + state fields) → Task 3 ✔
- [ ] Spec §4.5 (CommandService integration) → Task 8 ✔
- [ ] Spec §4.6 (DI) → Task 7 ✔
- [ ] Spec §4.7 (Settings) → Task 6 ✔
- [ ] Spec §5 (Fake API + Makefile) → Task 13 ✔
- [ ] Spec §6 (Error handling) → Tasks 1, 8, 9, 10, 11 ✔
- [ ] Spec §7 (Logging) → Task 8 (extra fields in log lines) ✔
- [ ] Spec §8 (Migration notes) → Task 14 ✔
- [ ] Spec §9 (Testing) → Tasks 4, 5, 10, 11, 12 ✔
- [ ] Spec §10 (Documentation) → Task 14 ✔
