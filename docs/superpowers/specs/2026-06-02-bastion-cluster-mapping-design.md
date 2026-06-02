# Bastion Cluster Mapping — Design

Date: 2026-06-02
Scope: `deploy-service/`

## 1. Background & Goal

`host_type=bastion` currently resolves the SSH target by calling the Inventory
API (`GET /inventory/hosts/{hostname}`) and using `info.bastion.ip` directly.
This is being replaced by a two-step resolution:

```text
node_name ──/api/v1/vms──▶ cluster_name ──/api/v1/bastion-cluster-mappings──▶ bastion_ip
                                              (regex match, top-priority wins)
```

The new path lets operators express "which bastion is responsible for which
cluster" declaratively (by pattern), and lets the same `node_name` map to
different bastions for different operational purposes via a `type` selector
(e.g., a separate bastion network for read-only inspection vs. mutating ops).

Goal: replace the bastion resolution path end-to-end, add the two backing
fake-API endpoints for local development, and keep the external HTTP
contract for `POST /api/v1/command/execution` backwards compatible.

## 2. Fake-API Additions

`fake-api/main.py` gains two new endpoints. The existing
`/inventory/hosts/{hostname}` endpoint is left in place for this change (the
`InventoryRepository` cleanup is deferred to a follow-up).

### 2.1 `GET /api/v1/vms?name={node_name}`

- Reads `data/vms-{node_name}.json`.
- If the file does not exist, reads `data/vms-not-found.json` (which contains
  `{count: 0, results: []}`).
- Always returns HTTP 200. "Not found" is conveyed by an empty `results`
  array, matching the shape of the fake-data fixture.
- Requires `Authorization` header (401 if missing) — same policy as the
  existing inventory endpoint.

### 2.2 `GET /api/v1/bastion-cluster-mappings?type={name}`

- Reads `data/bastion-cluster-mappings-{name}.json`.
- If the file does not exist, reads `data/bastion-cluster-mappings-not-found.json`
  (empty `results`).
- Always returns HTTP 200; empty `results` means "type not configured".
- Requires `Authorization` header.

### 2.3 Fixture fix

`data/bastion-cluster-mappings-type1.json` and `-type2.json` currently contain
patterns like `"type1-cluster*"` and `"type2-cluster*"`. Under `re.fullmatch`
the `*` is a regex quantifier on the preceding `r`, not a glob wildcard, so
those patterns would not match `type1-cluster-c1`. Patch the fixtures to use
`"type1-cluster.*"` and `"type2-cluster.*"` so they match the operator's
intent (which is what the second `pattern` entry in each `results[*]` was
clearly trying to express).

## 3. Repository Layer

Two new files under `app/repositories/`, each following the existing
`InventoryRepository` pattern (ABC + HTTP-backed implementation; `httpx` is
encapsulated and never leaks to the service layer).

### 3.1 `vm_repository.py`

```python
class VmK8sCluster(BaseModel):
    id: int
    name: str

class VmInfo(BaseModel):
    id: int
    name: str
    k8s_cluster: VmK8sCluster = Field(alias="k8s-cluster")

class VmRepository(ABC):
    @abstractmethod
    async def lookup_by_name(self, node_name: str) -> VmInfo: ...

class HttpVmRepository(VmRepository):
    # GET {base_url}/api/v1/vms?name={node_name}
    # 200 + exactly 1 result          → VmInfo (results[0])
    # 200 + empty results             → NotFoundException
    # 200 + >1 results                → UpstreamUnavailableException
    #                                   (invariant violation: name lookup
    #                                    must be unique; upstream is wrong)
    # 4xx (other) / 5xx               → UpstreamUnavailableException
    # timeout                         → UpstreamTimeoutException
    # network error                   → UpstreamUnavailableException
```

Note the `Field(alias="k8s-cluster")` — the upstream JSON key contains a
hyphen, so Pydantic needs an explicit alias. The model uses
`populate_by_name=True` so Python-side construction by attribute still works
in tests.

### 3.2 `bastion_mapping_repository.py`

```python
class BastionMapping(BaseModel):
    pattern: List[str]
    runner: str
    bastion: str
    bastion_ip: str

class BastionMappingRepository(ABC):
    @abstractmethod
    async def list_mappings(self, type_name: str) -> List[BastionMapping]: ...

class HttpBastionMappingRepository(BastionMappingRepository):
    # GET {base_url}/api/v1/bastion-cluster-mappings?type={type_name}
    # 200 + non-empty results        → list[BastionMapping] (priority preserved)
    # 200 + empty results             → NotFoundException (unknown type)
    # 4xx (other) / 5xx               → UpstreamUnavailableException
    # timeout                         → UpstreamTimeoutException
    # network error                   → UpstreamUnavailableException
```

Both repositories share the same base URL/token/timeout settings (see §6),
since the fake-API serves both. No shared base class — the two APIs are
independent contracts and could in principle be served by different upstreams
in the future.

## 4. Host Resolver Rewrite

`app/repositories/host_resolver.py`:

### 4.1 Delete `BastionHostResolver`

Per the migration decision, `host_type=bastion` no longer consults the
Inventory API. The old class is removed entirely (not kept as a fallback).

### 4.2 Add `ClusterBastionHostResolver`

```python
class ClusterBastionHostResolver(HostResolver):
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
```

Match semantics:

- `re.fullmatch` — pattern must match the entire `cluster_name` string.
- Outer loop iterates `results` in order (priority preserved by the upstream).
- Inner loop iterates `pattern` list of the current entry in order.
- First hit wins; no further evaluation.

### 4.3 Factory signature update

```python
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
        return HostnameHostResolver(inventory)
    if host_type == HostType.BASTION:
        return ClusterBastionHostResolver(vm_repo, mapping_repo, bastion_type)
    raise ValueError(f"Unsupported host_type: {host_type}")
```

Keyword-only args after `host_type` keep the call site readable and let
callers pass only the dependencies they actually need.

## 5. Domain & API Layer

### 5.1 `CommandOption` gains `bastion_type`

`app/domain/command.py`:

```python
class CommandOption(BaseModel):
    timeout_seconds: int = 30
    bastion_type: Optional[str] = None   # NEW
```

The HTTP route `POST /api/v1/command/execution` is unchanged — no query
parameters added, no top-level body fields added. Old clients that omit
`option.bastion_type` continue to work and fall through to the default.

### 5.2 Default resolution

In `CommandService._prepare_execution`:

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

For `host_type != BASTION`, `bastion_type` is ignored by the factory — the
value is still computed but never read.

### 5.3 Kill path is unaffected

`kill_command` reads `state.resolved_ip` to reconnect (already a bastion IP),
so the cross-pod kill flow does not need to re-resolve. No changes there.

## 6. Configuration

`app/core/config.py` — add four new settings:

```python
# ── Cluster / Bastion mapping API ─────────────────────────────────────────
CLUSTER_API_URL: str = "http://localhost:9001"
CLUSTER_API_TOKEN: str = "fake-cluster-token"
CLUSTER_API_TIMEOUT_SECONDS: float = 5.0
BASTION_DEFAULT_TYPE: str = "type1"
```

The same fake-API process serves both the vms and the bastion-mapping
endpoints, so a single base URL is enough.

Env files to update:

- `.env.dev` — same defaults, document next to the existing `INVENTORY_*` block.
- `.env.test` — short timeout, deterministic token, `BASTION_DEFAULT_TYPE=type1`.
- `.env` / `.env.prod` — leave for the operator to fill in (consistent with
  `INVENTORY_API_TOKEN` handling).

## 7. Dependency Injection

`app/core/dependencies.py`:

- Add `get_vm_repository()` returning `HttpVmRepository(...)`.
- Add `get_bastion_mapping_repository()` returning
  `HttpBastionMappingRepository(...)`.
- Update `get_command_service()` to inject the two new repos alongside the
  existing `inventory` dep.

`CommandService.__init__` gains `vm_repo: Optional[VmRepository]` and
`mapping_repo: Optional[BastionMappingRepository]` parameters. Optional so
unit tests that only exercise non-bastion paths can pass `None`.

## 8. Error Handling Summary

| Failure point                                | Exception                         | HTTP |
|----------------------------------------------|-----------------------------------|------|
| `node_name` not in vms (empty results)       | `NotFoundException`               | 404  |
| `vms` returns >1 result for a single `name`  | `UpstreamUnavailableException`    | 502  |
| `type` unknown (empty mappings)              | `NotFoundException`               | 404  |
| No `pattern` matches `cluster_name`          | `NotFoundException` (with detail) | 404  |
| Fake-API timeout                             | `UpstreamTimeoutException`        | 504  |
| Fake-API network/5xx                         | `UpstreamUnavailableException`    | 502  |

All exceptions are subclasses of `BaseAppException` and rendered by the
global handler in `main.py` using the existing structured-error shape.

## 9. Testing

### 9.1 Unit

- `tests/unit/test_vm_repository.py` — `httpx.MockTransport` driving the
  outcomes (200 single-result ok, 200 empty → `NotFoundException`,
  200 multi-result → `UpstreamUnavailableException`, 5xx, timeout). Verify
  `Authorization: Bearer …` header is sent.
- `tests/unit/test_bastion_mapping_repository.py` — same coverage matrix.
- `tests/unit/test_cluster_bastion_resolver.py`:
  - first-pattern hit
  - second-entry hit (priority preserved)
  - no match → `NotFoundException` with `cluster_name` in `detail`
  - empty vms → `NotFoundException`
  - `fullmatch` boundary: `type1-cluster.*` matches `type1-cluster-c1` /
    `type1-cluster-c99`; `type1-cluster-(c1|c2|c3)` matches `…-c1` but not
    `…-c99`
- `tests/unit/test_command_service.py` (extend):
  - `option.bastion_type="type2"` → resolver receives `"type2"`
  - `option.bastion_type=None` → resolver receives `BASTION_DEFAULT_TYPE`

### 9.2 Integration

- `tests/integration/test_command_bastion_flow.py` — full `TestClient` POST
  through `/api/v1/command/execution` with `host_type=bastion`. `httpx.MockTransport`
  mocks the fake-API. `asyncssh.connect` is patched to assert the target IP
  equals the expected `bastion_ip` (no real SSH).

### 9.3 Fake-API

- `tests/unit/test_fake_api.py` (new or extend):
  - known node / type → returns fixture payload
  - unknown node / type → returns the `*-not-found.json` payload, HTTP 200
  - missing Authorization → 401

## 10. Backwards Compatibility

- `/api/v1/command/execution` URL unchanged. No query parameters.
- Request body schema is additive: `option.bastion_type` is optional with a
  `None` default. Old clients keep working — they implicitly use
  `BASTION_DEFAULT_TYPE`.
- `host_type=ip` and `host_type=hostname` clients are untouched.
- `host_type=bastion` clients see a behaviour change (the resolver now
  consults vms + mappings instead of the inventory bastion field) — this is
  the intentional migration. The old `data/inventory.json` fixture remains
  in place; operators relying on it must point their bastion lookups at the
  new mappings file.

## 11. Documentation

- Update `deploy-service/ssh-command.md`:
  - rewrite the "Host resolution — `host_type=bastion`" section to describe
    the new two-step chain
  - document `option.bastion_type` and the `BASTION_DEFAULT_TYPE` fallback
  - note that the regex match uses `re.fullmatch` and that patterns are
    evaluated in priority order

## 12. Out of Scope

- Deleting `InventoryRepository` / the `/inventory/hosts/{hostname}` fake
  endpoint. Deferred so this change stays focused on the bastion path.
- Caching of `vms` / `mappings` responses. Add later if hot-path latency
  warrants it.
- A management endpoint to list available `bastion_type` values. Operators
  manage the JSON files directly for now.
