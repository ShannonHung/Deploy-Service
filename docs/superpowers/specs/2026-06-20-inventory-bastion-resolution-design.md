# Inventory Bastion Resolution Endpoint

**Date:** 2026-06-20  
**Scope:** `deploy-service/`

## Problem

Currently there are two separate inventory endpoints:
- `GET /api/v1/inventory/nodes/{node_name}` → node + cluster info
- `GET /api/v1/inventory/mappings?type=<type>` → bastion mapping list

To debug which bastion runner a node maps to, you must manually call both endpoints and apply the pattern-matching logic yourself. The goal is a single endpoint that does the full resolution and returns all intermediate data for easy debugging.

## Endpoint

```
GET /api/v1/inventory/nodes/{node_name}/bastion-resolution
```

**Query params:**
- `bastion_type` (optional) — overrides the `BASTION_NODE_TYPE_MAP` config lookup

**Auth:** `command_api` scope (same as existing inventory endpoints)

## Response Model

New model `NodeBastionResolution` added to `app/repositories/inventory_repository.py`, composing existing models:

```python
class NodeBastionResolution(BaseModel):
    node_type: str                                        # from ClusterNodeInfo
    node: NodeInfo                                        # reused
    cluster: ClusterRef                                   # reused
    bastion_type: str                                     # resolved type
    bastion_type_source: Literal["config", "query_param"] # how bastion_type was determined
    matched_mapping: BastionMapping                       # reused — the winning mapping entry
    matched_pattern: str                                  # the specific pattern that matched
```

Wrapped in `ApiResponse[NodeBastionResolution]` per the unified response shape convention.

## Architecture

Follows the existing `router → service → repository` layered pattern.

### New: `app/services/inventory_service.py`

```python
class InventoryService:
    def __init__(self, repo: InventoryRepository, node_type_map: Dict[str, str]) -> None: ...

    async def resolve_node_bastion(
        self,
        node_name: str,
        bastion_type_override: Optional[str] = None,
    ) -> NodeBastionResolution: ...
```

Resolution logic (mirrors `ClusterBastionHostResolver.resolve()` but returns full debug data):
1. `repo.lookup_by_name(node_name)` → `ClusterNodeInfo`
2. Determine `bastion_type`: use `bastion_type_override` if provided (`source="query_param"`), else look up `node_type` in `node_type_map` (`source="config"`)
3. `repo.list_mappings(bastion_type)` → `List[BastionMapping]`
4. Iterate mappings → patterns; first `re.fullmatch(pattern, cluster_name)` wins
5. Return `NodeBastionResolution` with all collected data

Error cases:
- Node not found → `NotFoundException` (propagated from repo)
- `node_type` not in `node_type_map` → `CommandExecutionException`
- No pattern matched → `NotFoundException`
- Invalid regex in mapping data → log warning, skip (same behaviour as `host_resolver.py`)

### DI factory in `app/core/dependencies.py`

```python
async def get_inventory_service(
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> InventoryService:
    s = get_settings()
    return InventoryService(repo=repo, node_type_map=s.BASTION_NODE_TYPE_MAP)
```

### Router addition in `app/api/v1/inventory.py`

```python
@router.get(
    "/nodes/{node_name}/bastion-resolution",
    response_model=ApiResponse[NodeBastionResolution],
    summary="Resolve node name to bastion runner",
)
async def get_node_bastion_resolution(
    request: Request,
    node_name: str,
    bastion_type: Optional[str] = Query(default=None),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
    service: InventoryService = Depends(get_inventory_service),
) -> ApiResponse[NodeBastionResolution]:
    data = await service.resolve_node_bastion(node_name, bastion_type_override=bastion_type)
    return ApiResponse(data=data, request_id=_request_id(request))
```

## Files Changed

| File | Change |
|------|--------|
| `app/repositories/inventory_repository.py` | Add `NodeBastionResolution` model |
| `app/services/inventory_service.py` | New file — `InventoryService` |
| `app/core/dependencies.py` | Add `get_inventory_service` factory |
| `app/api/v1/inventory.py` | Add `GET /nodes/{node_name}/bastion-resolution` route |
| `tests/unit/test_inventory_service.py` | New file — unit tests |
| `tests/integration/test_inventory_resolution.py` | New file — integration tests |

## Tests

**Unit** (`tests/unit/test_inventory_service.py`):
- `bastion_type_source="config"` when no override given
- `bastion_type_source="query_param"` when override given
- `node_type` not in `node_type_map` → `CommandExecutionException`
- No pattern matched → `NotFoundException`
- Invalid regex in mapping data is skipped (warning logged), next pattern tried

**Integration** (`tests/integration/test_inventory_resolution.py`):
- Happy path: `GET /api/v1/inventory/nodes/node1/bastion-resolution` → 200 with full resolution
- With `?bastion_type=xxx` override → `bastion_type_source="query_param"`
- Node not found → 404
- No mapping match → 404
