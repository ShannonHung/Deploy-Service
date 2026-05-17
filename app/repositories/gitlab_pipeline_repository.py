"""
app/repositories/gitlab_pipeline_repository.py

GitLab-backed implementation of PipelineRepository using python-gitlab.

Error handling strategy:
  - All ``gitlab.exceptions.GitlabError`` subclasses are caught here and
    re-raised as ``GitlabOperationException`` so the service / router layer
    never needs to import the GitLab SDK.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import gitlab
import gitlab.exceptions
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import (
    GitlabOperationException,
    NotFoundException,
    UpstreamTimeoutException,
)
from app.domain.pipeline_models import (
    DownstreamPipelineRef,
    JobData,
    PipelineData,
    PipelineVariable,
)
from app.repositories.pipeline_repository import PipelineRepository
from app.repositories.trace_cache_repository import TraceCacheRepository

_logger = logging.getLogger(__name__)

# Pipeline statuses that represent an in-progress (not yet finished) run.
_ACTIVE_STATUSES = [
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
]

# Job statuses for which the trace is guaranteed immutable and safe to cache.
_TERMINAL_JOB_STATUSES = frozenset(
    {"success", "failed", "canceled", "skipped"}
)


class GitlabPipelineRepository(PipelineRepository):
    """Uses python-gitlab to talk to the GitLab Pipelines API."""

    def __init__(
        self,
        url: str,
        token: str,
        project_id: int,
        trace_cache: TraceCacheRepository | None = None,
    ) -> None:
        self._gl = gitlab.Gitlab(url=url, private_token=token)
        self._project_id = project_id
        self._trace_cache = trace_cache

    # ── private helpers ───────────────────────────────────────────────────────

    def _get_project(self) -> Any:
        try:
            return self._gl.projects.get(self._project_id)
        except gitlab.exceptions.GitlabAuthenticationError as exc:
            # Token is invalid or expired — surface as a clear 401 rather
            # than the generic 502 GitlabOperationException.
            _logger.error(
                "GitLab authentication failed | project=%s | %s",
                self._project_id, exc,
            )
            raise GitlabOperationException(
                "GitLab authentication failed — GITLAB_TOKEN is invalid or expired.",
                detail=str(exc),
            ) from exc
        except gitlab.exceptions.GitlabGetError as exc:
            _logger.error("Failed to get project | id=%s | %s", self._project_id, exc)
            raise GitlabOperationException(
                f"GitLab project {self._project_id} not accessible.",
                detail=str(exc),
            ) from exc

    def _collect_job_tags(self, project: Any, pipeline_id: int) -> list[str]:
        """Return deduplicated runner tags from all pipeline jobs."""
        try:
            jobs = project.pipelines.get(pipeline_id).jobs.list()
            tags: set[str] = set()
            for job in jobs:
                tags.update(getattr(job, "tag_list", []))
            return sorted(tags)
        except gitlab.exceptions.GitlabError:
            return []   # tag_list is best-effort; don't fail the whole request

    def _collect_jobs(self, project: Any, pipeline_id: int) -> list[JobData]:
        """Return all jobs associated with the pipeline."""
        try:
            jobs = project.pipelines.get(pipeline_id).jobs.list()
            return [
                JobData(
                    id=job.id,
                    name=job.name,
                    status=job.status,
                )
                for job in jobs
            ]
        except gitlab.exceptions.GitlabError:
            return []  # jobs are best-effort; don't fail the whole request

    def _collect_variables(self, project: Any, pipeline_id: int) -> list[PipelineVariable]:
        """Return all variables the pipeline was triggered with."""
        try:
            raw = project.pipelines.get(pipeline_id).variables.list()
            return [PipelineVariable(key=v.key, value=v.value) for v in raw]
        except gitlab.exceptions.GitlabError:
            return []

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
        except (gitlab.exceptions.GitlabError, KeyError, TypeError):
            return []

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

    # ── interface implementation ───────────────────────────────────────────────

    async def trigger(self, ref: str, variables: dict[str, str]) -> PipelineData:
        project = self._get_project()
        gl_vars = [{"key": k, "value": v} for k, v in variables.items()]
        try:
            pipeline = project.pipelines.create({"ref": ref, "variables": gl_vars})
            _logger.info(
                "Pipeline triggered | id=%s | ref=%s | vars=%s",
                pipeline.id, ref, list(variables.keys()),
            )
            return self._to_pipeline_data(pipeline, project)
        except gitlab.exceptions.GitlabCreateError as exc:
            _logger.error("Pipeline trigger failed | ref=%s | %s", ref, exc)
            raise GitlabOperationException(
                "Failed to trigger GitLab pipeline.",
                detail=str(exc),
            ) from exc

    async def get(self, pipeline_id: int) -> PipelineData:
        project = self._get_project()
        try:
            pipeline = project.pipelines.get(pipeline_id)
            return self._to_pipeline_data(pipeline, project)
        except gitlab.exceptions.GitlabGetError as exc:
            if "404" in str(exc):
                raise NotFoundException(
                    f"Pipeline {pipeline_id} not found.",
                ) from exc
            raise GitlabOperationException(
                f"Failed to fetch pipeline {pipeline_id}.",
                detail=str(exc),
            ) from exc

    async def cancel(self, pipeline_id: int) -> PipelineData:
        project = self._get_project()
        try:
            pipeline = project.pipelines.get(pipeline_id)
            pipeline.cancel()
            pipeline = project.pipelines.get(pipeline_id)   # refresh state
            _logger.info("Pipeline cancelled | id=%s", pipeline_id)
            return self._to_pipeline_data(pipeline, project)
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to cancel pipeline {pipeline_id}.",
                detail=str(exc),
            ) from exc

    async def retry(self, pipeline_id: int) -> PipelineData:
        project = self._get_project()
        try:
            pipeline = project.pipelines.get(pipeline_id)
            pipeline.retry()
            pipeline = project.pipelines.get(pipeline_id)   # refresh state
            _logger.info("Pipeline retried | id=%s", pipeline_id)
            return self._to_pipeline_data(pipeline, project)
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to retry pipeline {pipeline_id}.",
                detail=str(exc),
            ) from exc

    async def list_running(self, ref: str) -> list[PipelineData]:
        """Return all active-state pipelines on *ref*.

        GitLab’s list endpoint only accepts a single status per request, so we
        issue one request per active status and deduplicate by ID.
        Variable details are fetched for each pipeline so the caller can do
        exact variable-matching without extra round-trips.
        """
        project = self._get_project()
        seen_ids: set[int] = set()
        result: list[PipelineData] = []

        try:
            for status in _ACTIVE_STATUSES:
                page = project.pipelines.list(
                    status=status,
                    ref=ref,
                    get_all=False,
                    per_page=50,
                )
                for p in page:
                    if p.id in seen_ids:
                        continue
                    seen_ids.add(p.id)
                    # Fetch full pipeline object to get variables & metadata.
                    full = project.pipelines.get(p.id)
                    result.append(self._to_pipeline_data(full, project))
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to list running pipelines on ref='{ref}'.",
                detail=str(exc),
            ) from exc

        return result

    async def get_job_web_url(self, job_id: int) -> str:
        def _fetch() -> str:
            project = self._get_project()
            return project.jobs.get(job_id).web_url

        try:
            return await run_in_threadpool(_fetch)
        except gitlab.exceptions.GitlabGetError as exc:
            if "404" in str(exc):
                raise NotFoundException(f"Job {job_id} not found.") from exc
            raise GitlabOperationException(
                f"Failed to fetch web URL for job {job_id}.",
                detail=str(exc),
            ) from exc
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to fetch web URL for job {job_id}.",
                detail=str(exc),
            ) from exc

    async def get_job_trace_range(
        self, job_id: int, byte_offset: int
    ) -> tuple[str, str, int]:
        """Return ``(status, new_text, total_size)`` for the trace tail.

        GitLab's trace endpoint does not honor HTTP Range headers — it always
        returns the full body. We therefore enforce *byte_offset* server-side
        by slicing locally. To avoid re-downloading the full trace on every
        poll once a job has finished, terminal-status traces are cached in
        Redis (gzip-compressed). Subsequent polls of a finished job are
        served entirely from cache with zero GitLab requests.
        """
        settings = get_settings()

        # ── Fast path: finished-job trace served from cache ───────────────
        if self._trace_cache is not None:
            cached = await self._trace_cache.get(self._project_id, job_id)
            if cached is not None:
                cached_status, cached_bytes = cached
                tail = (
                    cached_bytes[byte_offset:] if byte_offset else cached_bytes
                )
                return (
                    cached_status,
                    tail.decode("utf-8", errors="replace"),
                    len(cached_bytes),
                )

        def _fetch() -> tuple[str, bytes, bytes]:
            """Return ``(status, full_trace_bytes, tail_bytes)``."""
            project = self._get_project()
            job = project.jobs.get(job_id)
            path = f"{job.manager.path}/{job.encoded_id}/trace"
            # GitLab ignores Range headers on this endpoint; we always get
            # the full body back and slice locally below.
            resp = self._gl.http_get(path, raw=True, streamed=False)
            full = resp.content
            tail = full[byte_offset:] if byte_offset else full
            return job.status, full, tail

        try:
            status, full, tail = await asyncio.wait_for(
                run_in_threadpool(_fetch),
                timeout=settings.GITLAB_TRACE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise UpstreamTimeoutException(
                f"GitLab trace fetch for job {job_id} exceeded "
                f"{settings.GITLAB_TRACE_TIMEOUT_SECONDS}s.",
            ) from exc
        except gitlab.exceptions.GitlabGetError as exc:
            if "404" in str(exc):
                raise NotFoundException(f"Job {job_id} not found.") from exc
            raise GitlabOperationException(
                f"Failed to fetch trace range for job {job_id}.",
                detail=str(exc),
            ) from exc
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to fetch trace range for job {job_id}.",
                detail=str(exc),
            ) from exc

        # Write cache once the job is terminal. The trace is immutable from
        # this point on, so future polls bypass GitLab entirely.
        if (
            self._trace_cache is not None
            and status in _TERMINAL_JOB_STATUSES
            and full
        ):
            try:
                await self._trace_cache.set(
                    self._project_id,
                    job_id,
                    status,
                    full,
                    settings.GITLAB_TRACE_CACHE_TTL_SECONDS,
                )
            except Exception as exc:
                _logger.warning(
                    "Trace cache write failed | project=%s job=%s | %s",
                    self._project_id, job_id, exc,
                )

        return status, tail.decode("utf-8", errors="replace"), len(full)
