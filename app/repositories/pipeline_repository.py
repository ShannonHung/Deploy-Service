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
    async def get_job_web_url(self, job_id: int) -> str:
        """Return the GitLab UI URL for *job_id*.

        Uses the authoritative ``web_url`` field GitLab attaches to every
        job object, so the path-with-namespace (``group/project/-/jobs/id``)
        is always correct regardless of how the project is renamed or
        moved.
        """

    @abstractmethod
    async def get_job_trace_range(
        self, job_id: int, byte_offset: int
    ) -> tuple[str, str, int]:
        """Return the trace tail starting at *byte_offset*.

        Returns ``(status, new_text, total_size)``. ``total_size`` is the
        byte length of the full trace as of this read — callers use it as
        the next ``byte_offset`` for the following poll.

        Note: GitLab's trace endpoint does not honor HTTP Range headers,
        so implementations always read the whole body from upstream and
        slice locally. Implementations may cache immutable finished-job
        traces (terminal status) to avoid re-fetching on every poll.
        """
