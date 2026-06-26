# Host Type Resolver — Design

**Date:** 2026-05-18
**Status:** Approved (pending implementation plan)
**Scope:** `deploy-service/`

## 1. Background

`POST /api/v1/command/execution` currently treats the `host` field as a literal IP and SSHs straight to it. We need to support multiple ways of identifying the target machine:

| host_type   | `host` field means | Final SSH connection target           |
|-------------|--------------------|---------------------------------------|
| `ip`        | An IP address      | `host` (unchanged from today)         |
| `hostname`  | A machine name     | The machine's IP from Inventory       |
| `bastion`   | A machine name     | The IP of that machine's bastion node |

The list will grow. We want a clean strategy interface so adding a new host type does not touch `CommandService`.

`hostname` / `bastion` resolution needs an external **Inventory API**. The real Inventory is reachable only from inside the corporate network, so a tiny local fake service is provided for development.

## 2. Goals

- Allow callers to pick a host-resolution strategy via a `host_type` field.
- Keep the existing `ip` flow exactly as it is — `host_type` defaults to `ip` so old clients are unaffected.
- Provide a pluggable `HostResolver` interface so future types are a one-class + one-factory-line change.
- Provide a single-file fake Inventory API with a JSON data file the developer can edit by hand.
- Fix existing endpoint error-handling: replace "200 + failed body" for input/policy errors with proper 4xx HTTP status codes, so observability tooling and clients can distinguish success from failure correctly.

## 3. Non-goals

- Caching of inventory lookups (every request calls Inventory).
- Building the real Inventory API.
- Supporting multi-hop bastion chains.
- Changing the SSH execution / kill / state machine internals beyond what the new fields require.

## 4. Architecture

### 4.1 Layering

```
router (command.py)
  └─ CommandService.execute_command
       ├─ _prepare_execution
       │    ├─ load whitelist
       │    ├─ HostResolver.resolve(raw_host)   ← NEW
       │    │     └─ InventoryRepository.lookup (for hostname/bastion)
       │    ├─ allow/deny check on RESOLVED IP   ← changed
       │    └─ argument validation
       ├─ _build_pipeline
       ├─ _connect             ← uses resolved IP
       └─ _handle_async_execution / _handle_fire_and_forget
            └─ persists resolved_ip on CommandState
                  (cross-pod kill uses resolved_ip directly)
```

The resolver layer sits between the router and the SSH-execution layer. `CommandService` never sees Inventory directly; it sees a `ResolvedHost` value.

### 4.2 HostResolver interface

```python
# app/repositories/host_resolver.py
class ResolvedHost(BaseModel):
    ip: str                          # SSH target IP
    source_input: str                # original user input (for logging/debug)
    metadata: dict[str, str] = {}    # optional extras (e.g. bastion_hostname)

class HostResolver(ABC):
    @abstractmethod
    async def resolve(self, raw_host: str) -> ResolvedHost: ...

class IpHostResolver(HostResolver):
    """Returns the input unchanged."""

class HostnameHostResolver(HostResolver):
    """Looks raw_host up in Inventory, returns info.ip."""
    def __init__(self, inventory: InventoryRepository): ...

class BastionHostResolver(HostResolver):
    """Looks raw_host up in Inventory, returns info.bastion.ip."""
    def __init__(self, inventory: InventoryRepository): ...

def create_host_resolver(
    host_type: HostType,
    inventory: InventoryRepository,
) -> HostResolver:
    """Factory mirroring the pattern in ssh_auth_repository.create_authenticator."""
```

Adding a new host type later means: write one resolver class, add one branch to the factory. `CommandService` is untouched.

### 4.3 Inventory repository

```python
# app/repositories/inventory_repository.py
class InventoryBastion(BaseModel):
    hostname: str
    ip: str

class InventoryHostInfo(BaseModel):
    hostname: str
    ip: str
    bastion: InventoryBastion

class InventoryRepository(ABC):
    @abstractmethod
    async def lookup(self, hostname: str) -> InventoryHostInfo: ...

class HttpInventoryRepository(InventoryRepository):
    """GET {INVENTORY_API_URL}/inventory/hosts/{hostname}
       Header: Authorization: Bearer {INVENTORY_API_TOKEN}

       Maps responses to exceptions:
         200          → InventoryHostInfo
         404          → NotFoundException
         401/403      → UpstreamUnavailableException
         5xx          → UpstreamUnavailableException
         timeout      → UpstreamTimeoutException
         conn error   → UpstreamUnavailableException
    """
```

Implementation uses `httpx.AsyncClient`. No caching.

### 4.4 Request / state shape changes

```python
class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"

class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    host_type: HostType = HostType.IP   # NEW — defaults preserve current behaviour
    port: int = 22
    username: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)

class CommandState(BaseModel):
    ...
    host: str                # raw user input (kept for log clarity)
    host_type: HostType      # NEW
    resolved_ip: str         # NEW — used directly by cross-pod kill
    port: int
    ...
```

`ExecutionContext` also carries `resolved_host: ResolvedHost`.

The `port` and `username` fields always describe the **final SSH target** — i.e. for `host_type=bastion` they are the bastion's SSH port and the bastion's SSH user. There is no separate "target host inside the bastion"; the resolver only chooses which machine to SSH into.

### 4.4.1 Poll response — extra metadata fields

`GET /api/v1/command/execution/{command_id}` returns `CommandExecutionResponse`. It gains three optional fields surfaced from `CommandState`, so callers polling for status get the same operational metadata that lives in Redis:

```python
class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None
    exec_command: Optional[str] = None
    # NEW — populated only by the GET poll path:
    host_type: Optional[HostType] = None
    resolved_ip: Optional[str] = None
    pgids: List[int] = Field(default_factory=list)
```

These fields are **only set by `get_command_execution_result`** (the GET poll path). The initial POST `/command/execution` response (which only conveys `command_id` + `status: running`) does not populate them — `pgids` is empty at that moment anyway, and the caller already supplied `host_type`. The `failed()` / `success()` factory classmethods are unchanged.

### 4.5 CommandService integration

`_prepare_execution` becomes `async` and:

1. Loads whitelist.
2. Builds a resolver via `create_host_resolver(req.host_type, self.inventory)`.
3. Calls `resolved = await resolver.resolve(req.host)`.
4. Runs allow/deny match against `resolved.ip` (not `req.host`).
5. Validates command + arguments as today.
6. Loads SSH config.
7. Returns `ExecutionContext(..., resolved_host=resolved)`.

`_connect` reads `context.resolved_host.ip` instead of `req.host`.

`_handle_async_execution` writes `host_type` and `resolved_ip` into the persisted `CommandState`.

`kill_command` (cross-pod path) uses `state.resolved_ip` to reconnect; it does not call Inventory again. This makes kill robust to Inventory being down.

### 4.6 Dependency injection

```python
# app/core/dependencies.py
async def get_inventory_repository() -> InventoryRepository:
    return HttpInventoryRepository(
        base_url=settings.INVENTORY_API_URL,
        token=settings.INVENTORY_API_TOKEN,
        timeout=settings.INVENTORY_API_TIMEOUT_SECONDS,
    )

async def get_command_service(
    repo: CommandStateRepository = Depends(get_command_state_repository),
    inventory: InventoryRepository = Depends(get_inventory_repository),
) -> CommandService:
    return CommandService(repo, inventory)
```

`CommandService.__init__` gains an `inventory: InventoryRepository` parameter.

### 4.7 Settings additions

```python
# app/core/config.py
INVENTORY_API_URL: str = "http://localhost:9001"
INVENTORY_API_TOKEN: str = "fake-inventory-token"
INVENTORY_API_TIMEOUT_SECONDS: int = 5
```

Defaults match the local fake API. Prod env files override.

## 5. Fake Inventory API

Lives in `deploy-service/fake-api/` and shares the deploy-service venv. Not part of the deploy-service Python package — it is a sibling app started by a Makefile target.

### 5.1 Layout

```
deploy-service/
└── fake-api/
    ├── main.py
    └── data/
        └── inventory.json
```

### 5.2 `fake-api/main.py`

```python
from fastapi import FastAPI, HTTPException, Header
import json, pathlib

app = FastAPI(title="Fake Inventory API")
_DATA_PATH = pathlib.Path(__file__).parent / "data" / "inventory.json"

@app.get("/inventory/hosts/{hostname}")
def lookup(hostname: str, authorization: str | None = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization")
    records = json.loads(_DATA_PATH.read_text())
    for record in records:
        if record.get("hostname") == hostname:
            return record
    raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")
```

Auth: any non-empty `Authorization` header passes. Missing header → 401.

The JSON file is read fresh on every request so manual edits take effect without restarting.

### 5.3 `fake-api/data/inventory.json`

List of records, shaped to match what a real Inventory API would return:

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

### 5.4 Makefile target

```makefile
inventory-api:
	APP_ENV=dev uv run uvicorn fake-api.main:app --reload --port 9001
```

Run alongside `make dev` in a separate terminal.

## 6. Error handling

This release also tightens error handling on existing endpoints: previously many user-input and policy failures returned `200` with a `status: failed` body, which:
- Looks like success to standard HTTP tooling (gateway logs, monitoring, retry middleware).
- Forces clients to parse the response body to know if the call succeeded.
- Distorts error-rate metrics.

The full mapping:

| Situation                                                  | Old             | New                | Exception                       |
|------------------------------------------------------------|-----------------|--------------------|---------------------------------|
| Command name not in user's whitelist                       | 200 failed      | **403**            | `ForbiddenException`            |
| Host blocked by `deny_hosts` / not in `allow_hosts`        | 200 failed      | **403**            | `ForbiddenException`            |
| Missing required argument                                  | 200 failed      | **400**            | `CommandExecutionException`     |
| Argument fails `validation_regex`                          | 200 failed      | **400**            | `CommandExecutionException`     |
| Anti-injection detects dangerous chars                     | 200 failed      | **400**            | `CommandExecutionException`     |
| User whitelist file missing                                | 200 failed      | **403**            | `ForbiddenException`            |
| SSH config file missing                                    | 200 failed      | **500**            | `BaseAppException`              |
| Capacity full (`COMMAND_MAX_RUNNING`)                      | 200 failed      | **503**            | new `ServiceUnavailableException` (status 503) |
| Inventory: hostname not found                              | (new)           | **404**            | `NotFoundException`             |
| Inventory: timeout                                         | (new)           | **504**            | `UpstreamTimeoutException`      |
| Inventory: 401/403/connection failure                      | (new)           | **502**            | `UpstreamUnavailableException`  |
| SSH connect timeout                                        | 504             | 504 (unchanged)    | `UpstreamTimeoutException`      |
| SSH connect refused / auth fail / DNS                      | 502             | 502 (unchanged)    | `UpstreamUnavailableException`  |
| `host_type` outside enum                                   | (new)           | 422                | Pydantic                        |
| `GET /command/execution/{id}` not found                    | 404             | **404**            | `NotFoundException`             |
| `POST /command/execution/{id}/kill` on non-running state   | 200 failed      | **409**            | `ConflictException`             |
| Command executed (any exit_code)                           | 200             | 200 (unchanged)    | —                               |

The existing `BaseAppException` framework already returns the structured JSON shape `{"error": {"code", "message"}, "request_id": ...}` with the correct status code, so router code becomes simpler — no manual try/except wrapping.

### 6.1 Service / router code shape

`CommandService.execute_command` becomes a thin orchestrator that lets exceptions propagate:

```python
async def execute_command(self, username, request_id, req):
    self._check_capacity(username, request_id)        # raises 503
    context = await self._prepare_execution(...)       # raises 4xx
    context.pipeline_cmds = self._build_pipeline(context)
    conn = await self._connect(context, req)           # raises 5xx
    context.conn = conn
    if context.cmd_config.disconnects_ssh:
        return await self._handle_fire_and_forget(context)
    return await self._handle_async_execution(context)
```

Router endpoints drop their `try/except CommandExecutionException` blocks. The kill endpoint's "wrong state" branch raises `ConflictException` instead of returning a 200 failed body.

### 6.2 New exception class

Add to `app/core/exceptions.py`:

```python
class ServiceUnavailableException(BaseAppException):
    """Service temporarily cannot accept the request (capacity exhausted, etc.)."""
    http_status = 503
    error_code = "SERVICE_UNAVAILABLE"
    log_level = logging.WARNING
```

All other needed exception types (`ForbiddenException`, `NotFoundException`, `ConflictException`, `CommandExecutionException` at 400) already exist.

## 7. Logging

Connect / kill log lines lead with the resolved IP and tag both the type and raw input, e.g.:

```
SSH connection to 10.0.1.10:22 (host_type=hostname, raw=node-a01) timed out after 30s.
```

`CommandState`'s `host` (raw) and `resolved_ip` are both included in `extra` for structured-log indexing.

## 8. Migration / compatibility

- `host_type` defaults to `ip`. Clients that do not set it see no behaviour change in the success path.
- **Breaking change in failure paths:** clients that relied on `200 + status: failed` for whitelist/argument/host-policy errors must now handle the corresponding 4xx codes.
- `ssh-command.md` gains a "Migration notes" section listing the status-code changes.
- Existing integration tests need their expected status codes updated.

## 9. Testing

### 9.1 Unit

- `tests/unit/test_host_resolver.py` — three resolvers in isolation against an in-memory `FakeInventoryRepository`.
- `tests/unit/test_inventory_repository.py` — `HttpInventoryRepository` against an `httpx.MockTransport` (or `respx`): 200, 404, 401, 5xx, timeout, connection-error each map to the right exception.
- `tests/unit/test_command_service_errors.py` — confirms each failure path raises the right exception class (no more silent 200-failed responses).

### 9.2 Integration

- Inject an `InMemoryInventoryRepository` test fixture into the dependency override map.
- `tests/integration/test_command_host_type.py`:
  - `host_type=ip` → existing behaviour (smoke).
  - `host_type=hostname` → SSH layer is mocked, asserts `host` passed to asyncssh is `resolved.ip`.
  - `host_type=bastion` → asserts SSH target is `bastion.ip`.
  - `host_type=hostname` + unknown hostname → HTTP 404 with structured error body.
  - `host_type=bastion` + inventory timeout (fixture raises) → HTTP 504.
- Update existing tests that asserted `200 + status=failed` for whitelist/argument/host-policy failures to assert the new 4xx codes.

### 9.3 No tests for fake-api itself

It is a development convenience, not production code.

## 10. Documentation

- `ssh-command.md`:
  - New section "Host resolution" — describes `host_type`, the resolver factory pattern, and how to add a new type.
  - Updated "Errors" table reflecting the new HTTP status codes.
  - "Migration notes" section called out at the top of the doc.
- `deploy-service/README.md` (or `fake-api/README.md` if more natural): one paragraph + `make inventory-api` instruction.

## 11. Out-of-scope follow-ups

- Real Inventory client config (URL, token rotation, mTLS) — handled when wiring into prod environments.
- Inventory response caching — revisit if `_prepare_execution` latency becomes an issue.
- Multi-hop bastion (e.g. user → bastion A → bastion B → target). Current design is one bastion hop.
