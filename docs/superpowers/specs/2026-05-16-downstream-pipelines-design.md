# Expose downstream pipelines on PipelineData

**Status:** Approved, ready for implementation
**Date:** 2026-05-16
**Scope:** `deploy-service/`

## Problem

`GET /api/v1/deploy/stage/{pipeline_id}` returns a `PipelineData` whose `jobs` field only contains the parent pipeline's regular jobs. When the parent pipeline triggers a downstream pipeline (via GitLab's `trigger:` keyword), the downstream pipeline lives under a separate pipeline ID and its jobs are not visible from the parent. Callers cannot discover those downstream pipelines from the current response, so they cannot poll their status.

## Background

GitLab models downstream pipelines as a separate `Pipeline` linked to the parent via a **bridge job** (job kind `bridge`, not `build`). Bridges are returned by `project.pipelines.get(parent_id).bridges.list()` — not by `jobs.list()`. Each bridge carries a `downstream_pipeline` attribute (`{id, status, web_url, project_id, ...}`) once the trigger has fired; before that, the attribute is `None`.

Downstream pipelines may live in a different GitLab project (`trigger: project: other/repo`). Their `project_id` may differ from the parent's.

## Goal

Add a single new field `downstream_pipelines: list[DownstreamPipelineRef]` to `PipelineData`. Clients use the returned IDs to follow up with `GET /stage/{downstream_id}?project_id=<downstream_project_id>` themselves.

Non-goals:

- Recursive walking of downstream-of-downstream.
- Dedicated `/downstream` endpoint.
- Caching of bridge results.

## Design

### New domain model

In `app/domain/pipeline_models.py`:

```python
class DownstreamPipelineRef(BaseModel):
    """A downstream pipeline triggered by a bridge job in the parent pipeline."""
    id: int
    status: str
    web_url: str
    project_id: int
    bridge_name: str   # the bridge job's name, e.g. "trigger:deploy-prod"
```

Extend `PipelineData`:

```python
class PipelineData(BaseModel):
    ...existing fields...
    downstream_pipelines: list[DownstreamPipelineRef] = Field(
        default_factory=list,
        description="Downstream pipelines triggered by bridge jobs in this pipeline.",
    )
```

### Repository change

In `app/repositories/gitlab_pipeline_repository.py`, add one helper alongside the existing `_collect_*` helpers:

```python
def _collect_downstream_pipelines(
    self, project: Any, pipeline_id: int
) -> list[DownstreamPipelineRef]:
    """Return downstream pipelines triggered by bridge jobs in this pipeline."""
    try:
        bridges = project.pipelines.get(pipeline_id).bridges.list(get_all=True)
        result: list[DownstreamPipelineRef] = []
        for bridge in bridges:
            downstream = getattr(bridge, "downstream_pipeline", None)
            if not downstream:
                continue  # bridge hasn't fired yet
            result.append(DownstreamPipelineRef(
                id=downstream["id"],
                status=downstream["status"],
                web_url=downstream.get("web_url", ""),
                project_id=downstream["project_id"],
                bridge_name=getattr(bridge, "name", ""),
            ))
        return result
    except gitlab.exceptions.GitlabError:
        return []  # best-effort, matches existing pattern
```

Wire it into `_to_pipeline_data` so every endpoint that returns `PipelineData` (trigger, get, cancel, retry, check-running) populates the field uniformly.

### Behaviour

- **Pending bridges** (no `downstream_pipeline` yet) are silently omitted from the response.
- **Cross-project downstreams** are returned with their own `project_id`. The client uses that value on the follow-up `GET /stage/{id}?project_id=<x>` call; `_get_deploy_service(project_id)` already resolves the right token via `GitlabAuthRepository`.
- **Failure mode**: if `bridges.list()` raises `GitlabError`, the helper logs and returns `[]`. This matches `_collect_jobs`, `_collect_variables`, and `_collect_job_tags`. The supplemental field is not allowed to fail the primary status request.
- **Cost**: one extra GitLab call per pipeline serialized. Felt most on `check-running` (multiple pipelines listed), but `list_running` already does N+1 (`pipelines.get` per match), so this is proportional, not a step change. Acceptable for now.

### Affected endpoints

All endpoints that return `PipelineData` will include the new field:

- `POST /api/v1/deploy/stage` (trigger)
- `GET /api/v1/deploy/stage/{pipeline_id}` (status)
- `POST /api/v1/deploy/stage/{pipeline_id}/cancel`
- `POST /api/v1/deploy/stage/{pipeline_id}/retry`
- `POST /api/v1/deploy/stage/check-running` (each pipeline in the list)

For newly triggered pipelines the field is normally `[]` (bridges haven't fired yet); it populates on subsequent status polls.

## Testing

Repository-level tests in `tests/unit/`:

- `_collect_downstream_pipelines` returns the populated list when bridges have downstream pipelines.
- Bridges with `downstream_pipeline=None` are filtered out.
- Cross-project downstream: returned entry carries the downstream's own `project_id`, not the parent's.
- `GitlabError` from `bridges.list()` results in an empty list (no exception propagated).

Service-level test:

- `DeployService.get_pipeline` round-trips `downstream_pipelines` from the fake repo through to the caller unchanged.

Integration: no new integration test required — existing deploy-route tests will still pass because the new field defaults to `[]`.

## Risks and mitigations

| Risk                                                                          | Mitigation                                                                                                                                                  |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bridge API call adds latency to every `PipelineData` response                 | Best-effort + same-pattern N+1 as existing helpers; revisit with caching if measurements show it matters                                                    |
| Client breakage from new field                                                | Field is additive with a default of `[]`; clients ignore unknown fields by default                                                                          |
| Cross-project downstreams confuse clients                                     | `project_id` is part of the ref so the client always has what it needs to make the follow-up call                                                           |
| `downstream_pipeline` dict shape changes in a future python-gitlab version    | All field reads use `[]` for required keys and `.get(..., "")` for optional ones; behind a try/except that returns `[]` on any `GitlabError`                |
