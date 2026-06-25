# Design: `cluster` host type, cluster bastion-resolution endpoint, and admin running-jobs endpoint

Date: 2026-06-24
Status: Approved (brainstorming) — pending implementation plan

## Overview

Three additive features for `deploy-service/`:

- **A.** A new `cluster` host type. Resolve a **cluster_name directly** (no node-lookup) to a bastion IP. Whether the cluster_name contains a `/` decides the `bastion_type`, which is then used against the **existing** inventory `list_mappings(type)` regex-match path.
- **B.** A new resolution endpoint `GET /api/v1/inventory/cluster/bastion-resolution?cluster_name=<name>` exposing the same resolution as a read-only query (scope `command_api`).
- **C.** A new admin-only endpoint `GET /api/v1/command/running` that lists in-flight commands from Redis across all pods, so an operator can decide whether it is safe to start an upgrade. Gated by a new `admin_api` scope.

All three are additive: no existing `HostType`, endpoint, or behaviour changes.

## Context (existing machinery being reused / contrasted)

- `HostType` enum (`app/domain/command.py`): `IP`, `BASTION`, `HOSTNAME`.
- `host_resolver.py`: `IpHostResolver`, `HostnameHostResolver`, `ClusterBastionHostResolver`, and the `create_host_resolver()` factory. The existing `BASTION` path resolves **node_name → inventory `lookup_by_name` → cluster → `list_mappings(type)` → regex match**, where `type` comes from `BASTION_NODE_TYPE_MAP[node_type]`.
- `InventoryRepository.list_mappings(type_name)` returns `List[BastionMapping]` (each with `patterns`, `runner`, `bastion`, `bastion_ip`).
- `InventoryService.resolve_node_bastion(...)` powers the existing `GET /inventory/nodes/{node_name}/bastion-resolution`.
- `CommandStateRepository` (Redis) keys commands at `command:{command_id}`; values are `CommandState` JSON. `CommandState.status` is a `CommandStatus` (`running`, `killing`, `killed`, `success`, `failed`).
- Existing scopes: `deploy_api`, `vm_api`, `command_api`. Admin user is `admin` (`data/users.json`) / `test_admin` (`tests/fixtures/users.json`).
- Test fixture `tests/fixtures/cluster.py::InMemoryInventoryRepository` and integration override of `get_inventory_repository`.

**Key contrast:** the new `cluster` path does NOT call `lookup_by_name`. It takes the cluster_name as direct input and derives `bastion_type` purely from slash-presence. The two paths converge only at `list_mappings(type)` + regex match.

## Feature A — `cluster` host type

### Domain
Add `CLUSTER = "cluster"` to `HostType` (`app/domain/command.py`).

### Config
Add to `app/core/config.py`:
```python
# Maps slash-presence of a cluster_name to a bastion_type. Keys MUST be
# "no_slash" and "with_slash". Example:
#   CLUSTER_SLASH_TYPE_MAP='{"no_slash": "type1", "with_slash": "type2"}'
CLUSTER_SLASH_TYPE_MAP: Dict[str, str] = {}
```

### Shared helper
A single function so the resolver (Feature A) and the service (Feature B) cannot drift. In `host_resolver.py`:
```python
def cluster_type_from_name(cluster_name: str, slash_map: Dict[str, str]) -> tuple[str, bool]:
    """Return (bastion_type, has_slash). Slash-presence selects the key
    ("with_slash" or "no_slash") in slash_map. Raises CommandExecutionException
    with a clear message if the required key is missing from config."""
```
- `has_slash = "/" in cluster_name`
- key = `"with_slash"` if `has_slash` else `"no_slash"`
- missing key → `CommandExecutionException` naming the missing key and showing the current map (mirrors the existing `BASTION_NODE_TYPE_MAP` error style).

### Resolver
New `ClusterNameResolver(HostResolver)` in `host_resolver.py`:
```python
class ClusterNameResolver(HostResolver):
    def __init__(self, inventory_repo, slash_map): ...
    async def resolve(self, raw_host: str) -> ResolvedHost:
        # raw_host IS the cluster_name
        bastion_type, has_slash = cluster_type_from_name(raw_host, self._slash_map)
        mappings = await self._inventory_repo.list_mappings(bastion_type)
        # regex fullmatch raw_host against each mapping.patterns (reuse the exact
        # loop + invalid-regex-skip logic from ClusterBastionHostResolver)
        # match  → ResolvedHost(ip=mapping.bastion_ip, source_input=raw_host,
        #          metadata={cluster_name, bastion_type, has_slash, bastion_hostname, matched_pattern})
        # no match → NotFoundException(cluster_name, bastion_type)
```

### Factory wiring
In `create_host_resolver()` add:
```python
if host_type == HostType.CLUSTER:
    if inventory_repo is None or slash_map is None:
        raise ValueError("CLUSTER resolver requires inventory_repo and slash_map")
    return ClusterNameResolver(inventory_repo, slash_map)
```
Add a `slash_map: Optional[Dict[str, str]] = None` keyword param to `create_host_resolver()`.

### CommandService wiring
In `_prepare_execution` (`command_service.py`), the existing `create_host_resolver(...)` call must pass `slash_map=settings.CLUSTER_SLASH_TYPE_MAP`. For `host_type=cluster`, `req.host` carries the cluster_name. No other change to the execution flow — allow/deny matching, argument validation, SSH, kill, log viewer all operate on the resolved IP exactly as today.

> Note: for `host_type=cluster`, the request's `host` field holds the cluster_name (which may contain a slash). This is request-body JSON, so the slash is unproblematic. `bastion_type`/`ip_label` options are irrelevant to this resolver and are ignored.

## Feature B — cluster bastion-resolution endpoint

### Route (query-param form)
Because cluster_name may contain `/`, the resolution endpoint takes it as a query param rather than a path segment:
```
GET /api/v1/inventory/cluster/bastion-resolution?cluster_name=<name>
```
Scope: `command_api` (consistent with the other inventory endpoints). Added to `app/api/v1/inventory.py`.

> Deviation from the originally-phrased path `/inventory/cluster/<cluster_name>/bastion-resolution`: a slash in cluster_name collides with path parsing. Query-param form was chosen during brainstorming. The route is registered before/separately from any `/cluster/{...}` path route to avoid ambiguity.

### Response model
New `ClusterBastionResolution` in `inventory_repository.py`:
```python
class ClusterBastionResolution(BaseModel):
    cluster_name: str
    has_slash: bool
    bastion_type: str
    matched_mapping: BastionMapping
    matched_pattern: str
```
(No `node`/`node_type`/`bastion_type_source` — those come from node-lookup, which this path skips.)

### Service
New `InventoryService.resolve_cluster_bastion(cluster_name)`:
- `bastion_type, has_slash = cluster_type_from_name(cluster_name, self._slash_map)` (the SAME shared helper from Feature A)
- `mappings = await self._repo.list_mappings(bastion_type)`
- regex-fullmatch loop (same invalid-regex-skip behaviour) → `ClusterBastionResolution`
- no match → `NotFoundException(cluster_name, bastion_type)`

`InventoryService.__init__` gains `slash_map: Dict[str, str]`. The DI factory `get_inventory_service` passes `slash_map=s.CLUSTER_SLASH_TYPE_MAP`.

## Feature C — admin running-jobs endpoint

### Scope
New `admin_api` scope. Add to:
- `data/users.json` → `admin`
- `tests/fixtures/users.json` → `test_admin`

### Route
```
GET /api/v1/command/running?status=<optional CommandStatus>
```
Added to `app/api/v1/command.py`. Gated by `Depends(get_current_user(["admin_api"]))`.
- Default (no `status`): returns all **non-terminal** jobs — `running` + `killing`.
- `?status=<value>`: narrows to a single status (any valid `CommandStatus`; invalid value → 422 from enum validation).

### Repository
New `CommandStateRepository.list_states(statuses: Optional[set[CommandStatus]] = None) -> list[CommandState]`:
- enumerate keys with `self.redis.scan_iter(match="command:*")` — cursor-based, non-blocking across all pods (NOT `KEYS`).
- load each value into `CommandState`; on parse failure, log a warning and skip (don't fail the whole listing).
- if `statuses` given, keep only matching; else return all loaded.

### Service
New `CommandService.list_running_commands(statuses: Optional[set[CommandStatus]] = None) -> list[CommandState]`:
- default `statuses = {RUNNING, KILLING}` when `None`.
- delegates to `repo.list_states(statuses)`.

### Response model
New in `app/domain/command.py`:
```python
class RunningCommandsResponse(BaseModel):
    count: int
    commands: List[CommandState]
```
Endpoint returns `ApiResponse[RunningCommandsResponse]`. Full `CommandState` records are returned as-is — they carry no secrets (SSH keys live in separate `SSH-*.json` files; only the `ssh_config` *name* is in state), and the endpoint is admin-gated.

## Testing

Follows the existing split: unit tests mock repos; integration tests use `TestClient` with `app.dependency_overrides`.

### Unit
- `test_cluster_name_resolver.py` — `ClusterNameResolver`: no-slash→type1, slash→type2, regex match, no-match→`NotFoundException`, missing slash-map key→`CommandExecutionException`. Mock `list_mappings`.
- `test_cluster_type_from_name.py` (or fold into the above) — the shared helper in isolation: slash/no-slash key selection + missing-key error.
- `test_inventory_service.py` (extend) — `resolve_cluster_bastion`: happy path (slash + no-slash), no-match→404, missing-key error. Use `InMemoryInventoryRepository`.
- `test_command_state_list.py` — `list_states`: status filtering, skips unparseable records. Mock `scan_iter` + `get` (or use a fake Redis).
- `test_command_service_running.py` — `list_running_commands`: default = running+killing, explicit status set passthrough.

### Integration
- `test_cluster_bastion_resolution_api.py` — `GET /inventory/cluster/bastion-resolution`: 200 (no-slash → type1 mapping), 200 (slash → type2 mapping), 404 no-match, 401/403 without `command_api`. Override `get_inventory_repository` with `InMemoryInventoryRepository`; set `CLUSTER_SLASH_TYPE_MAP` via settings override.
- `test_command_running_api.py` — `GET /command/running`: 200 for `test_admin` (`admin_api`), **403 for a `command_api`-only user** (proves the admin gate), `?status=` filter narrows results, `count` matches. Seed Redis state via the command state repo (or mock the service).

### Config for tests
`CLUSTER_SLASH_TYPE_MAP` must be set in the test environment (e.g. `.env.test` or per-test settings override with `get_settings.cache_clear()`), e.g. `{"no_slash": "type1", "with_slash": "type2"}`.

## Out of scope / YAGNI
- No pagination on `/command/running` (operator-scale lists; `scan_iter` already streams).
- No change to the existing `BASTION`/`HOSTNAME`/node bastion-resolution paths.
- No new SSH/kill/log logic — the `cluster` host type plugs into the existing post-resolution flow unchanged.

## Files touched (summary)
- `app/domain/command.py` — `HostType.CLUSTER`, `RunningCommandsResponse`.
- `app/core/config.py` — `CLUSTER_SLASH_TYPE_MAP`.
- `app/repositories/host_resolver.py` — `cluster_type_from_name`, `ClusterNameResolver`, factory branch + `slash_map` param.
- `app/repositories/inventory_repository.py` — `ClusterBastionResolution`.
- `app/repositories/command_state_repository.py` — `list_states`.
- `app/services/inventory_service.py` — `resolve_cluster_bastion`, `slash_map` in `__init__`.
- `app/services/command_service.py` — pass `slash_map` in `_prepare_execution`; `list_running_commands`.
- `app/core/dependencies.py` — pass `slash_map` to `get_inventory_service`.
- `app/api/v1/inventory.py` — cluster bastion-resolution route.
- `app/api/v1/command.py` — `/command/running` route.
- `data/users.json`, `tests/fixtures/users.json` — `admin_api` scope.
- `app/api/router.py` — docstring route layout update.
- Tests as listed above.
