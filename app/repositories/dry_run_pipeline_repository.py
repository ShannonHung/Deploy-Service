"""
app/repositories/dry_run_pipeline_repository.py

Dry-run implementation of PipelineRepository — returns canned data and never
talks to GitLab.

Used only when ``DRY_RUN_MODE=true`` (e2e pipeline testing). It is substituted
for ``GitlabPipelineRepository`` at the single DI factory in the deploy router,
so everything above the repository boundary runs unchanged: routing, JWT +
scope checks, Pydantic validation, EXECUTION / SERVICE_FROM variable injection,
and the duplicate-detection comparison in ``DeployService``.

That is the whole point of the seam: the e2e test exercises the real service
logic and only the outermost side effect is stubbed. See
docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.domain.pipeline_models import JobData, PipelineData, PipelineVariable
from app.repositories.pipeline_repository import PipelineRepository

_logger = logging.getLogger(__name__)

# Synthetic ids are deliberately implausible as real GitLab records. A dry-run
# id that leaks into a real system should fail loudly and immediately rather
# than collide with a genuine pipeline and propagate quietly.
_DRY_RUN_PIPELINE_ID = 999001
_DRY_RUN_JOB_ID = 999101

_DRY_RUN_WEB_URL = "https://dry-run.invalid/pipelines/999001"


class DryRunPipelineRepository(PipelineRepository):
    """Canned PipelineRepository used when DRY_RUN_MODE is enabled.

    Every method satisfies the same contract as GitlabPipelineRepository so the
    service layer cannot tell the difference, but no network call is ever made
    and no GitLab token is required.
    """

    def __init__(self, ref: str = "main") -> None:
        # Only used so a synthesised pipeline echoes a plausible ref when the
        # caller does not supply one (get / cancel / retry take an id, not a ref).
        self._default_ref = ref

    # ── helpers ───────────────────────────────────────────────────────────────

    def _pipeline(
        self,
        *,
        status: str,
        ref: str | None = None,
        variables: dict[str, str] | None = None,
        pipeline_id: int = _DRY_RUN_PIPELINE_ID,
        with_jobs: bool = False,
    ) -> PipelineData:
        """Build a schema-valid PipelineData with obviously synthetic values."""
        now = datetime.now(timezone.utc)
        return PipelineData(
            id=pipeline_id,
            status=status,
            created_at=now,
            updated_at=now,
            started_at=now if status != "created" else None,
            finished_at=None,
            tag_list=["dry-run"] if with_jobs else [],
            variables=[
                PipelineVariable(key=k, value=v) for k, v in (variables or {}).items()
            ],
            jobs=(
                [JobData(id=_DRY_RUN_JOB_ID, name="dry-run-job", status=status)]
                if with_jobs
                else []
            ),
            downstream_pipelines=[],
            ref_name=ref or self._default_ref,
            web_url=_DRY_RUN_WEB_URL,
        )

    # ── interface implementation ──────────────────────────────────────────────

    async def trigger(self, ref: str, variables: dict[str, str]) -> PipelineData:
        """Pretend to trigger a pipeline.

        The returned variables echo exactly what the service built, so an e2e
        assertion can verify that EXECUTION and SERVICE_FROM injection actually
        happened rather than trusting that the code path ran.
        """
        _logger.warning(
            "DRY-RUN | op=pipelines.create | ref=%s | variables=%s "
            "| no GitLab pipeline was triggered",
            ref,
            variables,
        )
        return self._pipeline(status="created", ref=ref, variables=variables)

    async def get(self, pipeline_id: int) -> PipelineData:
        _logger.info("DRY-RUN | op=pipelines.get | pipeline_id=%s", pipeline_id)
        return self._pipeline(
            status="running", pipeline_id=pipeline_id, with_jobs=True
        )

    async def cancel(self, pipeline_id: int) -> PipelineData:
        _logger.warning(
            "DRY-RUN | op=pipelines.cancel | pipeline_id=%s | nothing was cancelled",
            pipeline_id,
        )
        return self._pipeline(status="canceled", pipeline_id=pipeline_id)

    async def retry(self, pipeline_id: int) -> PipelineData:
        _logger.warning(
            "DRY-RUN | op=pipelines.retry | pipeline_id=%s | nothing was retried",
            pipeline_id,
        )
        return self._pipeline(status="pending", pipeline_id=pipeline_id)

    async def list_running(self, ref: str) -> list[PipelineData]:
        """Always report no active pipelines.

        Returning an empty list is deliberate: ``DeployService`` still runs its
        real ``_variables_match`` comparison over this result, so a regression
        in duplicate detection is still detectable, but a dry-run trigger is
        never blocked by a spurious 409 ConflictException.
        """
        _logger.debug("DRY-RUN | op=pipelines.list_running | ref=%s", ref)
        return []

    async def get_job_web_url(self, job_id: int) -> str:
        return f"https://dry-run.invalid/jobs/{job_id}"

    async def get_job_trace_range(
        self, job_id: int, byte_offset: int
    ) -> tuple[str, str, int]:
        """Return a short canned trace.

        Honours *byte_offset* the same way the real implementation does, so a
        polling viewer terminates instead of appending the same text forever.
        """
        trace = (
            "[dry-run] This is a synthetic job trace.\n"
            "[dry-run] No GitLab job was executed.\n"
        )
        total = len(trace.encode("utf-8"))
        if byte_offset >= total:
            return "success", "", total
        return "success", trace[byte_offset:], total
