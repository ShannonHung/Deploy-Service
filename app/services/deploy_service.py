"""
app/services/deploy_service.py

Business logic for pipeline deployment operations.

Depends on PipelineRepository (abstract) — never imports GitLab SDK directly.
"""

from __future__ import annotations

import logging

from app.core.exceptions import ConflictException
from app.domain.pipeline_models import (
    PipelineVariable,
    RunningPipelinesData,
    FormattedLogResponse,
)
from app.core.log_renderer import LogRenderer
from app.repositories.pipeline_repository import PipelineRepository

_logger = logging.getLogger(__name__)


class DeployService:
    """Orchestrates pipeline trigger, status, cancel, and retry operations."""

    def __init__(self, pipeline_repo: PipelineRepository) -> None:
        self._repo = pipeline_repo

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_variables(
        self, action: str, extra_variables: list[PipelineVariable]
    ) -> dict[str, str]:
        """Merge EXECUTION + caller-supplied variables into a flat dict.

        EXECUTION is always set from *action*; extra variables are added
        afterwards so they cannot accidentally override EXECUTION.
        """
        variables: dict[str, str] = {"EXECUTION": action}
        for var in extra_variables:
            variables[var.key] = var.value
        return variables

    def _variables_match(
        self, pipeline: PipelineData, expected: dict[str, str]
    ) -> bool:
        """Return True if a pipeline was triggered with exactly *expected* variables."""
        actual = {v.key: v.value for v in pipeline.variables}
        return actual == expected

    # ── public API ────────────────────────────────────────────────────────────

    async def find_duplicate_pipelines(
        self,
        action: str,
        ref: str,
        extra_variables: list[PipelineVariable],
    ) -> RunningPipelinesData:
        """Return all active pipelines on *ref* that share the same variables.

        Matches on the full variable set (EXECUTION + extras) so pipelines
        triggered with different parameters on the same branch are NOT blocked.
        """
        target_vars = self._build_variables(action, extra_variables)
        running = await self._repo.list_running(ref=ref)

        duplicates = [p for p in running if self._variables_match(p, target_vars)]

        _logger.info(
            "Duplicate check | ref=%s | target_vars=%s | active=%d | matches=%d",
            ref, list(target_vars.keys()), len(running), len(duplicates),
        )
        return RunningPipelinesData(
            has_running=bool(duplicates),
            count=len(duplicates),
            pipelines=duplicates,
        )

    async def trigger_pipeline(
        self,
        action: str,
        ref: str,
        extra_variables: list[PipelineVariable],
    ) -> PipelineData:
        """Trigger a new pipeline after verifying no duplicate is already running.

        Raises:
            ConflictException: A running pipeline with identical parameters exists.
        """
        # ── Duplicate guard ───────────────────────────────────────────────────
        result = await self.find_duplicate_pipelines(action, ref, extra_variables)
        if result.has_running:
            existing = result.pipelines[0]
            raise ConflictException(
                f"A pipeline with identical parameters is already running "
                f"(id={existing.id}, status={existing.status}).",
                detail={
                    "pipeline_id": existing.id,
                    "status": existing.status,
                    "web_url": existing.web_url,
                },
            )

        # ── Trigger ───────────────────────────────────────────────────────────
        variables = self._build_variables(action, extra_variables)
        _logger.info(
            "Triggering pipeline | action=%s | ref=%s | extra_vars=%s",
            action, ref, [v.key for v in extra_variables],
        )
        return await self._repo.trigger(ref=ref, variables=variables)

    async def get_pipeline(self, pipeline_id: int) -> PipelineData:
        """Return the current state of a pipeline."""
        return await self._repo.get(pipeline_id)

    async def cancel_pipeline(self, pipeline_id: int) -> PipelineData:
        """Cancel a running pipeline."""
        _logger.info("Cancelling pipeline | id=%s", pipeline_id)
        return await self._repo.cancel(pipeline_id)

    async def retry_pipeline(self, pipeline_id: int) -> PipelineData:
        """Retry a failed or cancelled pipeline."""
        _logger.info("Retrying pipeline | id=%s", pipeline_id)
        return await self._repo.retry(pipeline_id)

    async def get_job_trace(self, job_id: int) -> tuple[str, str]:
        """Return the status and raw console output for a job."""
        return await self._repo.get_job_trace(job_id)

    async def get_formatted_job_trace(self, job_id: int, offset: int = 0) -> FormattedLogResponse:
        """Return processed HTML logs for the UI."""
        status, raw_text = await self._repo.get_job_trace(job_id)
        renderer = LogRenderer()
        lines = renderer.render(job_id, raw_text)
        
        next_offset = len(lines)
        returned_lines = lines[offset:] if offset < len(lines) else []
        
        return FormattedLogResponse(
            job_id=job_id,
            status=status,
            next_offset=next_offset,
            lines=returned_lines
        )
