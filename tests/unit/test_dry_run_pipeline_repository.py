"""
tests/unit/test_dry_run_pipeline_repository.py

T2: DryRunPipelineRepository must satisfy the PipelineRepository contract well
enough that DeployService cannot tell it apart from the GitLab-backed one.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import inspect

import pytest

from app.domain.pipeline_models import PipelineData
from app.repositories.dry_run_pipeline_repository import DryRunPipelineRepository
from app.repositories.pipeline_repository import PipelineRepository


@pytest.fixture
def repo() -> DryRunPipelineRepository:
    return DryRunPipelineRepository()


# ── contract conformance ──────────────────────────────────────────────────────


def test_implements_every_abstract_method():
    """A missing method would only surface as a TypeError at request time, so
    assert the whole surface up front."""
    abstract = {
        name
        for name, value in inspect.getmembers(PipelineRepository)
        if getattr(value, "__isabstractmethod__", False)
    }
    assert abstract, "expected PipelineRepository to declare abstract methods"
    assert not abstract - set(dir(DryRunPipelineRepository))
    # Instantiable == nothing left abstract.
    assert isinstance(DryRunPipelineRepository(), PipelineRepository)


async def test_trigger_returns_pipeline_data(repo):
    result = await repo.trigger(ref="main", variables={"EXECUTION": "deploy"})
    assert isinstance(result, PipelineData)
    assert result.ref_name == "main"
    assert result.status == "created"


async def test_trigger_echoes_variables(repo):
    """Echoing the variables lets an e2e test verify that EXECUTION /
    SERVICE_FROM injection actually happened, rather than trusting it did."""
    result = await repo.trigger(
        ref="main", variables={"EXECUTION": "deploy", "SERVICE_FROM": "alice"}
    )
    assert {v.key: v.value for v in result.variables} == {
        "EXECUTION": "deploy",
        "SERVICE_FROM": "alice",
    }


async def test_get_cancel_retry_echo_requested_id(repo):
    for coro in (repo.get(4242), repo.cancel(4242), repo.retry(4242)):
        result = await coro
        assert isinstance(result, PipelineData)
        assert result.id == 4242


async def test_list_running_is_always_empty(repo):
    """Duplicate detection still runs over this, but must never block a
    dry-run trigger with a spurious 409."""
    assert await repo.list_running(ref="main") == []


async def test_get_job_web_url_is_a_string(repo):
    assert isinstance(await repo.get_job_web_url(7), str)


# ── synthetic identifiers ─────────────────────────────────────────────────────


async def test_default_pipeline_id_is_obviously_synthetic(repo):
    """A dry-run id that leaks downstream should fail loudly rather than
    collide with a real pipeline."""
    result = await repo.trigger(ref="main", variables={})
    assert result.id == 999001


async def test_web_url_is_not_a_real_host(repo):
    result = await repo.trigger(ref="main", variables={})
    assert ".invalid" in result.web_url


# ── trace ─────────────────────────────────────────────────────────────────────


async def test_trace_returns_status_text_and_size(repo):
    status, text, total = await repo.get_job_trace_range(job_id=1, byte_offset=0)
    assert status == "success"
    assert text
    assert total == len(text.encode("utf-8"))


async def test_trace_honours_byte_offset(repo):
    """A polling viewer must terminate instead of appending the same canned
    text on every poll."""
    _, _, total = await repo.get_job_trace_range(job_id=1, byte_offset=0)
    status, text, total_again = await repo.get_job_trace_range(
        job_id=1, byte_offset=total
    )
    assert text == ""
    assert total_again == total
