"""
tests/unit/test_gitlab_pipeline_repository.py

Unit tests for GitlabPipelineRepository helpers that do not require
a real GitLab connection. The python-gitlab objects are mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import gitlab.exceptions

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


def test_collect_downstream_pipelines_returns_empty_on_gitlab_error():
    project = MagicMock()
    project.pipelines.get.return_value.bridges.list.side_effect = (
        gitlab.exceptions.GitlabListError("boom")
    )
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(project, pipeline_id=1)

    assert result == []


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
