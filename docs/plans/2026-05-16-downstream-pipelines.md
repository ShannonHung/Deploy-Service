# Downstream Pipelines on PipelineData — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose GitLab downstream pipelines on every `PipelineData` response so clients can poll downstream status.

**Architecture:** Add a `DownstreamPipelineRef` domain model and a `downstream_pipelines` field on `PipelineData`. Populate it in `GitlabPipelineRepository._to_pipeline_data` via a new best-effort `_collect_downstream_pipelines` helper that calls `project.pipelines.get(id).bridges.list()`. All existing endpoints inherit the new field automatically.

**Tech Stack:** Python 3.11, FastAPI, pydantic v2, python-gitlab, pytest (`asyncio_mode = "auto"`), `uv` for dep/venv management.

**Spec:** `deploy-service/docs/specs/2026-05-16-downstream-pipelines-design.md`

**Working directory for all commands:** `deploy-service/`

---

## File Structure

- **Modify:** `deploy-service/app/domain/pipeline_models.py` — add `DownstreamPipelineRef`, extend `PipelineData`.
- **Modify:** `deploy-service/app/repositories/gitlab_pipeline_repository.py` — add `_collect_downstream_pipelines`, wire into `_to_pipeline_data`.
- **Create:** `deploy-service/tests/unit/test_gitlab_pipeline_repository.py` — unit tests for the new helper.
- **Modify:** `deploy-service/tests/unit/test_auth_service.py` — no changes (mentioned only to confirm it's untouched).

Each task below produces a self-contained TDD cycle (failing test → minimal code → green → commit).

---

## Task 1: Add `DownstreamPipelineRef` domain model

**Files:**
- Modify: `deploy-service/app/domain/pipeline_models.py`
- Test: (covered by Task 3 — the helper test exercises this type)

This task is a pure data-model change. The next tasks will reference it.

- [ ] **Step 1: Add the new model class**

Open `deploy-service/app/domain/pipeline_models.py`. Locate the `JobData` class (currently around line 51). Insert the new class immediately after it:

```python
class DownstreamPipelineRef(BaseModel):
    """A downstream pipeline triggered by a bridge job in the parent pipeline."""

    id: int
    status: str
    web_url: str = ""
    project_id: int
    bridge_name: str = ""
```

- [ ] **Step 2: Extend `PipelineData` with the new field**

In the same file, find the `PipelineData` class. After the existing `jobs: list[JobData] = ...` field, add:

```python
    downstream_pipelines: list[DownstreamPipelineRef] = Field(
        default_factory=list,
        description="Downstream pipelines triggered by bridge jobs in this pipeline.",
    )
```

- [ ] **Step 3: Verify the file still imports cleanly**

Run:

```bash
cd deploy-service && APP_ENV=test uv run python -c "from app.domain.pipeline_models import PipelineData, DownstreamPipelineRef; print(PipelineData.model_fields['downstream_pipelines'])"
```

Expected output: a `FieldInfo` line that mentions `list[DownstreamPipelineRef]` and `default_factory=list`. No traceback.

- [ ] **Step 4: Confirm existing tests still pass**

Run:

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/ -v
```

Expected: all existing tests pass. The new field is additive with a default, so no test should break.

- [ ] **Step 5: Commit**

```bash
cd deploy-service && git add app/domain/pipeline_models.py
git commit -m "feat(domain): add DownstreamPipelineRef and downstream_pipelines field"
```

---

## Task 2: Failing test for `_collect_downstream_pipelines` — happy path

**Files:**
- Create: `deploy-service/tests/unit/test_gitlab_pipeline_repository.py`

This is the first test of `GitlabPipelineRepository`. It uses `unittest.mock` to fake the python-gitlab project/bridges objects so we exercise the helper without a real GitLab. The pattern mirrors `tests/unit/test_gitlab_auth_repository.py` (plain pytest, no fixtures beyond what's needed).

- [ ] **Step 1: Write the failing test file**

Create `deploy-service/tests/unit/test_gitlab_pipeline_repository.py` with the following content:

```python
"""
tests/unit/test_gitlab_pipeline_repository.py

Unit tests for GitlabPipelineRepository helpers that do not require
a real GitLab connection. The python-gitlab objects are mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import gitlab.exceptions
import pytest

from app.repositories.gitlab_pipeline_repository import GitlabPipelineRepository


def _make_repo() -> GitlabPipelineRepository:
    """Build a repo without hitting the network — the helper under test
    does not use the gitlab.Gitlab client directly; it only uses the
    *project* object that is passed into it."""
    return GitlabPipelineRepository(
        url="http://example.invalid",
        token="dummy",
        project_id=1,
    )


def _make_project_with_bridges(bridges: list) -> MagicMock:
    """Return a mocked python-gitlab project whose pipelines.get(<id>).bridges.list()
    returns the given list of bridge objects."""
    project = MagicMock()
    project.pipelines.get.return_value.bridges.list.return_value = bridges
    return project


def test_collect_downstream_pipelines_returns_fired_bridges():
    bridge = SimpleNamespace(
        name="trigger:deploy-prod",
        downstream_pipeline={
            "id": 999,
            "status": "running",
            "web_url": "http://gitlab.example/-/pipelines/999",
            "project_id": 42,
        },
    )
    project = _make_project_with_bridges([bridge])
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(project, pipeline_id=1)

    assert len(result) == 1
    entry = result[0]
    assert entry.id == 999
    assert entry.status == "running"
    assert entry.web_url == "http://gitlab.example/-/pipelines/999"
    assert entry.project_id == 42
    assert entry.bridge_name == "trigger:deploy-prod"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_collect_downstream_pipelines_returns_fired_bridges -v
```

Expected: FAIL with `AttributeError: 'GitlabPipelineRepository' object has no attribute '_collect_downstream_pipelines'`.

- [ ] **Step 3: Commit the failing test**

```bash
cd deploy-service && git add tests/unit/test_gitlab_pipeline_repository.py
git commit -m "test(repo): add failing test for _collect_downstream_pipelines happy path"
```

---

## Task 3: Implement `_collect_downstream_pipelines` (happy path)

**Files:**
- Modify: `deploy-service/app/repositories/gitlab_pipeline_repository.py`

- [ ] **Step 1: Add the `DownstreamPipelineRef` import**

Open `deploy-service/app/repositories/gitlab_pipeline_repository.py`. Find the existing import line:

```python
from app.domain.pipeline_models import JobData, PipelineData, PipelineVariable
```

Replace it with:

```python
from app.domain.pipeline_models import (
    DownstreamPipelineRef,
    JobData,
    PipelineData,
    PipelineVariable,
)
```

- [ ] **Step 2: Add the helper method**

In the same file, locate `_collect_variables` (the last `_collect_*` helper, currently ending around line 110). Insert the new helper immediately after it, before `_to_pipeline_data`:

```python
    def _collect_downstream_pipelines(
        self, project: Any, pipeline_id: int
    ) -> list[DownstreamPipelineRef]:
        """Return downstream pipelines triggered by bridge jobs in this pipeline.

        Best-effort: any GitLab error → empty list (matches the other
        ``_collect_*`` helpers). Bridges whose ``downstream_pipeline`` is
        ``None`` (trigger not yet fired) are omitted.
        """
        try:
            bridges = project.pipelines.get(pipeline_id).bridges.list(get_all=True)
            result: list[DownstreamPipelineRef] = []
            for bridge in bridges:
                downstream = getattr(bridge, "downstream_pipeline", None)
                if not downstream:
                    continue
                result.append(
                    DownstreamPipelineRef(
                        id=downstream["id"],
                        status=downstream["status"],
                        web_url=downstream.get("web_url", ""),
                        project_id=downstream["project_id"],
                        bridge_name=getattr(bridge, "name", ""),
                    )
                )
            return result
        except gitlab.exceptions.GitlabError:
            return []
```

- [ ] **Step 3: Run the test to verify it passes**

Run:

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_collect_downstream_pipelines_returns_fired_bridges -v
```

Expected: PASS.

- [ ] **Step 4: Confirm no regression in the full test suite**

Run:

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd deploy-service && git add app/repositories/gitlab_pipeline_repository.py
git commit -m "feat(repo): collect downstream pipelines from GitLab bridge jobs"
```

---

## Task 4: Test — pending bridges are filtered out

**Files:**
- Modify: `deploy-service/tests/unit/test_gitlab_pipeline_repository.py`

- [ ] **Step 1: Append the failing test**

Add to the bottom of `tests/unit/test_gitlab_pipeline_repository.py`:

```python
def test_collect_downstream_pipelines_skips_bridges_without_downstream():
    fired = SimpleNamespace(
        name="trigger:deploy-prod",
        downstream_pipeline={
            "id": 101,
            "status": "pending",
            "web_url": "http://gitlab.example/-/pipelines/101",
            "project_id": 42,
        },
    )
    pending = SimpleNamespace(
        name="trigger:deploy-staging",
        downstream_pipeline=None,
    )
    project = _make_project_with_bridges([fired, pending])
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(project, pipeline_id=1)

    assert len(result) == 1
    assert result[0].id == 101
    assert result[0].bridge_name == "trigger:deploy-prod"
```

- [ ] **Step 2: Run the test to verify it passes immediately**

The behaviour was already implemented in Task 3, so this test is a regression guard rather than a new red→green cycle.

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_collect_downstream_pipelines_skips_bridges_without_downstream -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add tests/unit/test_gitlab_pipeline_repository.py
git commit -m "test(repo): downstream collector skips bridges with no downstream"
```

---

## Task 5: Test — cross-project downstreams carry their own project_id

**Files:**
- Modify: `deploy-service/tests/unit/test_gitlab_pipeline_repository.py`

- [ ] **Step 1: Append the test**

Add to the bottom of `tests/unit/test_gitlab_pipeline_repository.py`:

```python
def test_collect_downstream_pipelines_preserves_cross_project_id():
    """A downstream in a different GitLab project must surface its own
    project_id so the client can route the follow-up call correctly."""
    bridge = SimpleNamespace(
        name="trigger:other-repo",
        downstream_pipeline={
            "id": 555,
            "status": "running",
            "web_url": "http://gitlab.example/other/-/pipelines/555",
            "project_id": 7777,
        },
    )
    project = _make_project_with_bridges([bridge])
    repo = _make_repo()  # parent project_id=1

    result = repo._collect_downstream_pipelines(project, pipeline_id=1)

    assert result[0].project_id == 7777
```

- [ ] **Step 2: Run the test to verify it passes**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_collect_downstream_pipelines_preserves_cross_project_id -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add tests/unit/test_gitlab_pipeline_repository.py
git commit -m "test(repo): downstream collector preserves cross-project project_id"
```

---

## Task 6: Test — GitLab errors are swallowed (best-effort)

**Files:**
- Modify: `deploy-service/tests/unit/test_gitlab_pipeline_repository.py`

- [ ] **Step 1: Append the test**

Add to the bottom of `tests/unit/test_gitlab_pipeline_repository.py`:

```python
def test_collect_downstream_pipelines_returns_empty_on_gitlab_error():
    project = MagicMock()
    project.pipelines.get.return_value.bridges.list.side_effect = (
        gitlab.exceptions.GitlabListError("boom")
    )
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(project, pipeline_id=1)

    assert result == []
```

- [ ] **Step 2: Run the test to verify it passes**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_collect_downstream_pipelines_returns_empty_on_gitlab_error -v
```

Expected: PASS — the existing `except gitlab.exceptions.GitlabError: return []` in Task 3 catches `GitlabListError` (a subclass).

- [ ] **Step 3: Commit**

```bash
cd deploy-service && git add tests/unit/test_gitlab_pipeline_repository.py
git commit -m "test(repo): downstream collector returns [] on GitLab error"
```

---

## Task 7: Wire the helper into `_to_pipeline_data`

**Files:**
- Modify: `deploy-service/app/repositories/gitlab_pipeline_repository.py`
- Modify: `deploy-service/tests/unit/test_gitlab_pipeline_repository.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_gitlab_pipeline_repository.py`:

```python
def test_to_pipeline_data_includes_downstream_pipelines():
    """_to_pipeline_data must populate downstream_pipelines on the returned PipelineData."""
    bridge = SimpleNamespace(
        name="trigger:deploy-prod",
        downstream_pipeline={
            "id": 999,
            "status": "running",
            "web_url": "http://gitlab.example/-/pipelines/999",
            "project_id": 42,
        },
    )
    project = MagicMock()
    # jobs/variables/tags are best-effort — let them return empty lists
    project.pipelines.get.return_value.jobs.list.return_value = []
    project.pipelines.get.return_value.variables.list.return_value = []
    project.pipelines.get.return_value.bridges.list.return_value = [bridge]

    pipeline_obj = SimpleNamespace(
        id=1,
        status="running",
        created_at="2026-05-16T00:00:00Z",
        updated_at="2026-05-16T00:00:00Z",
        started_at=None,
        finished_at=None,
        ref="main",
        web_url="http://gitlab.example/-/pipelines/1",
    )

    repo = _make_repo()
    data = repo._to_pipeline_data(pipeline_obj, project)

    assert len(data.downstream_pipelines) == 1
    assert data.downstream_pipelines[0].id == 999
    assert data.downstream_pipelines[0].bridge_name == "trigger:deploy-prod"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_to_pipeline_data_includes_downstream_pipelines -v
```

Expected: FAIL — assertion error `len(data.downstream_pipelines) == 1` is `0 == 1`, because the field is still defaulting to `[]`.

- [ ] **Step 3: Wire the helper into `_to_pipeline_data`**

Open `deploy-service/app/repositories/gitlab_pipeline_repository.py`. Find `_to_pipeline_data`. The current body builds a `PipelineData(...)` literal with `jobs=self._collect_jobs(project, pid)`. Add the new field to the same constructor call so the final form is:

```python
    def _to_pipeline_data(self, pipeline: Any, project: Any) -> PipelineData:
        """Map a python-gitlab Pipeline object → PipelineData."""
        pid: int = pipeline.id
        return PipelineData(
            id=pid,
            status=pipeline.status,
            created_at=getattr(pipeline, "created_at", None),
            updated_at=getattr(pipeline, "updated_at", None),
            started_at=getattr(pipeline, "started_at", None),
            finished_at=getattr(pipeline, "finished_at", None),
            tag_list=self._collect_job_tags(project, pid),
            variables=self._collect_variables(project, pid),
            jobs=self._collect_jobs(project, pid),
            downstream_pipelines=self._collect_downstream_pipelines(project, pid),
            ref_name=getattr(pipeline, "ref", ""),
            web_url=getattr(pipeline, "web_url", ""),
        )
```

(Only one line is added: `downstream_pipelines=self._collect_downstream_pipelines(project, pid),`.)

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/unit/test_gitlab_pipeline_repository.py::test_to_pipeline_data_includes_downstream_pipelines -v
```

Expected: PASS.

- [ ] **Step 5: Run the full suite — no regressions**

```bash
cd deploy-service && APP_ENV=test uv run pytest tests/ -v
```

Expected: all tests pass, including the existing integration tests in `tests/integration/test_deploy_dynamic_auth.py`. That suite patches `GitlabPipelineRepository` so it does not exercise the new code path, but it must continue to pass.

- [ ] **Step 6: Commit**

```bash
cd deploy-service && git add app/repositories/gitlab_pipeline_repository.py tests/unit/test_gitlab_pipeline_repository.py
git commit -m "feat(repo): include downstream pipelines in PipelineData responses"
```

---

## Task 8: Manual smoke check against a real GitLab pipeline (optional)

**Files:** none

This task is a manual sanity check using the existing dev server. Skip if you have no GitLab credentials handy; the unit tests already cover the behaviour.

- [ ] **Step 1: Start the dev server**

```bash
cd deploy-service && make dev
```

- [ ] **Step 2: Hit the status endpoint for a parent pipeline you know has triggered a downstream**

Use `deploy-service/rest_client/deploy.http` (or `curl`) to call `GET /api/v1/deploy/stage/{pipeline_id}` with a valid token and a pipeline ID known to have downstream pipelines.

Expected: the JSON response includes a non-empty `downstream_pipelines` array with `id`, `status`, `web_url`, `project_id`, `bridge_name` populated for each fired bridge. Bridges whose triggers haven't fired are not listed.

- [ ] **Step 3: Drill into one downstream**

Take an `id` and `project_id` from the array and call `GET /api/v1/deploy/stage/{id}?project_id={project_id}`. Confirm a normal `PipelineData` response comes back — proving the cross-project drill-in works end to end.

No commit for this task (no file changes).

---

## Self-Review Checklist

- **Spec coverage:**
  - `DownstreamPipelineRef` model — Task 1
  - `downstream_pipelines` field on `PipelineData` — Task 1
  - Best-effort `_collect_downstream_pipelines` helper — Task 3
  - Pending-bridge filtering — Task 4
  - Cross-project `project_id` preservation — Task 5
  - GitLab error → empty list — Task 6
  - Wired into every endpoint via `_to_pipeline_data` — Task 7
  - End-to-end sanity — Task 8

- **Placeholder scan:** no TBD/TODO; every code step shows the actual code; every command shows the actual invocation and expected output.

- **Type consistency:** `DownstreamPipelineRef` field names (`id`, `status`, `web_url`, `project_id`, `bridge_name`) used identically in Tasks 1, 3, 4, 5, 7. Helper name `_collect_downstream_pipelines` used identically across Tasks 3, 4, 5, 6, 7.
