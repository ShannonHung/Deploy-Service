"""
app/repositories/pipeline_repository.py

Abstract interface for pipeline operations.

Services depend on this interface — never on a concrete GitLab client.
Swap GitlabPipelineRepository for a mock in tests or a different SCM later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.pipeline_models import PipelineData


class PipelineRepository(ABC):
    """Abstract contract for CI/CD pipeline operations."""

    @abstractmethod
    async def trigger(
        self,
        ref: str,
        variables: dict[str, str],
    ) -> PipelineData:
        """Trigger a new pipeline on *ref* with *variables*.

        Args:
            ref:       Branch / tag name to run the pipeline on.
            variables: Flat key→value dict of pipeline variables.

        Returns:
            Full PipelineData response.
        """

    @abstractmethod
    async def get(self, pipeline_id: int) -> PipelineData:
        """Fetch the current state of an existing pipeline."""

    @abstractmethod
    async def cancel(self, pipeline_id: int) -> PipelineData:
        """Cancel a running pipeline and return its updated state."""

    @abstractmethod
    async def retry(self, pipeline_id: int) -> PipelineData:
        """Retry a failed/cancelled pipeline and return its new state."""

    @abstractmethod
    async def list_running(self, ref: str) -> list[PipelineData]:
        """Return all pipelines in active states (created / pending / running /
        waiting_for_resource / preparing) on *ref*.

        Used for duplicate-detection before triggering a new pipeline.
        """

    @abstractmethod
    async def get_job_trace(self, job_id: int) -> tuple[str, str]:
        """Fetch the current status and the raw log trace for a specific job."""
