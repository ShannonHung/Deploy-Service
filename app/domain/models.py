"""
app/domain/models.py

All Pydantic models for the application.

Layers:
  - Storage models  : shapes that match the JSON / DB store
  - Domain models   : business objects passed between layers
  - Request models  : validated HTTP request bodies
  - Response models : HTTP response payloads

Response design (REST-style):
  Success → {"data": <T>, "request_id": "...", "dry_run": false}
  Error   → {"error": {"code": "...", "message": "..."}, "request_id": "..."}

  HTTP status code carries the success/failure signal — no redundant
  "success" boolean or "error: null" in the body.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ──────────────────────────────────────────────────────────────────────────────
# Storage / domain
# ──────────────────────────────────────────────────────────────────────────────


class UserInDB(BaseModel):
    """Representation of a user as stored in the backing store.

    Fields must exactly match the keys in ``data/users.json``.
    Never expose this model directly in API responses.
    """

    account: str
    hashed_password: str
    scopes: list[str] = Field(default_factory=list)


class User(BaseModel):
    """Public domain model — safe to pass between layers and return in APIs."""

    account: str
    scopes: list[str] = Field(default_factory=list)


class TokenPayload(BaseModel):
    """JWT payload structure."""

    sub: str  # account name
    scopes: list[str] = Field(default_factory=list)
    exp: int | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────


class HashPasswordRequest(BaseModel):
    """Body for POST /api/v1/auth/hash-password."""

    password: str = Field(..., min_length=8, description="Plain-text password to hash")


# ──────────────────────────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────────────────────────


def _dry_run_default() -> bool:
    """Resolve the ``dry_run`` marker from settings.

    This is the single place the flag is read for responses — the ~21
    ``ApiResponse(...)`` construction sites across the app are deliberately left
    untouched, so the marker cannot be forgotten at a new one. Imported lazily
    to keep ``app.domain`` free of a module-level dependency on ``app.core``.
    """
    from app.core.config import get_settings

    return get_settings().DRY_RUN_MODE


class ApiResponse(BaseModel, Generic[T]):
    """Unified success response envelope.

    All successful endpoints return:
        {"data": <T>, "request_id": "uuid", "dry_run": false}

    HTTP 2xx status communicates success — no redundant ``success`` field.

    ``dry_run`` is true only when the service runs with ``DRY_RUN_MODE=true``,
    in which case no real side effect took place (no GitLab pipeline, no SSH
    command). It defaults to false, so adding it is not a breaking change for
    existing clients. The marker exists so a "successful" response can never be
    mistaken for real work having happened — see docs/arch/dry-run-mode.md.
    """

    data: T
    request_id: str = ""
    dry_run: bool = Field(
        default_factory=_dry_run_default,
        description=(
            "True when the service is running in dry-run mode and no real "
            "side effect was performed."
        ),
    )


class ErrorDetail(BaseModel):
    """Structured error payload for failed responses.

    Returned as:
        {"error": {"code": "...", "message": "..."}, "request_id": "uuid"}
    """

    code: str
    message: str
    detail: Any = None


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint-specific data payloads
# ──────────────────────────────────────────────────────────────────────────────


class TokenData(BaseModel):
    """Internal data payload used by AuthService when generating a token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class OAuth2TokenResponse(BaseModel):
    """Flat response for POST /token — OAuth2-standard structure.

    Swagger UI requires ``access_token`` and ``token_type`` at the TOP LEVEL
    of the response to auto-populate the Authorization header.  Other endpoints
    use the unified ``ApiResponse[T]`` envelope.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class VerifyData(BaseModel):
    """Data payload for GET /api/v1/auth/verify."""

    account: str
    scopes: list[str]
    valid: bool = True


class HashPasswordData(BaseModel):
    """Data payload for POST /api/v1/auth/hash-password."""

    hashed_password: str


class MyScopesData(BaseModel):
    """Data payload for GET /api/v1/auth/my-scopes."""

    account: str
    scopes: list[str]
