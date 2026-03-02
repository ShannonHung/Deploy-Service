"""
app/core/logging.py

Request-ID middleware and logging configuration.

Every inbound request gets a UUID4 request_id that is:
  - Stored on request.state.request_id
  - Injected into all log records via RequestIdFilter
  - Returned in response headers as X-Request-ID
"""

from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# ──────────────────────────────────────────────────────────────────────────────
# Log filter — injects request_id into every log record
# ──────────────────────────────────────────────────────────────────────────────

class RequestIdFilter(logging.Filter):
    """Logging filter that adds ``request_id`` to log records.

    When called outside of a request context the filter defaults to "N/A".
    Uses a module-level variable set by the middleware on each request.
    """

    _current_request_id: str = "N/A"

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = self._current_request_id  # type: ignore[attr-defined]
        return True


_request_id_filter = RequestIdFilter()


# ──────────────────────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────────────────────

class RequestIdMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that assigns a unique request_id to every request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Make request_id available to all log records within this request
        RequestIdFilter._current_request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ──────────────────────────────────────────────────────────────────────────────
# Setup helper
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with request_id in the format string.

    Call once in ``lifespan`` startup.
    """
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
