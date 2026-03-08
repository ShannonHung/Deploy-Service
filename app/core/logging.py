"""
app/core/logging.py

Request-ID middleware and logging configuration.

X-Coordination-ID behaviour:
  - If the incoming request has an X-Coordination-ID header, that value is used
    as the request_id and echoed back in X-Coordination-ID response header.
  - If the header is absent a UUID4 is generated internally for logging only;
    it is NOT added to the response headers (the caller didn't ask for one).
"""

from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

COORDINATION_ID_HEADER = "X-Coordination-ID"


# ──────────────────────────────────────────────────────────────────────────────
# Log filter — injects request_id into every log record
# ──────────────────────────────────────────────────────────────────────────────

class RequestIdFilter(logging.Filter):
    """Logging filter that adds ``request_id`` to log records."""

    _current_request_id: str = "N/A"

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = self._current_request_id  # type: ignore[attr-defined]
        return True


class HealthCheckFilter(logging.Filter):
    """Filter out /health access logs to reduce noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access logs store the formatted message or args
        return "/health" not in record.getMessage()


_request_id_filter = RequestIdFilter()
_health_filter = HealthCheckFilter()


# ──────────────────────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────────────────────

class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a request_id to every incoming request.

    Priority:
      1. X-Coordination-ID request header  → use as-is, echo in response header
      2. No header present                  → generate UUID4 for internal logging,
                                              do NOT add to response headers
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        coordination_id = request.headers.get(COORDINATION_ID_HEADER)
        from_client = coordination_id is not None
        request_id = coordination_id or str(uuid.uuid4())

        request.state.request_id = request_id
        RequestIdFilter._current_request_id = request_id

        response = await call_next(request)

        if from_client:
            # Echo back only when the client explicitly provided the header
            response.headers[COORDINATION_ID_HEADER] = request_id

        return response


# ──────────────────────────────────────────────────────────────────────────────
# Setup helper
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with request_id in the format string."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.addFilter(_request_id_filter)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | req=%(request_id)s | "
                "%(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # ── Uvicorn specific ───────────────────────────────────────────────────────
    # Filter out /health from access logs
    logging.getLogger("uvicorn.access").addFilter(_health_filter)
