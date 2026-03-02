"""
app/domain/models.py

All Pydantic models for the application.

Layers:
  - Storage models  : shapes that match the JSON / DB store
  - Domain models   : business objects passed between layers
  - Request models  : validated HTTP request bodies
  - Response models : HTTP response payloads (unified BaseResponse wrapper)
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

    sub: str          # account name
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

class ErrorDetail(BaseModel):
    """Structured error payload embedded inside BaseResponse on failure."""

    code: str
    message: str
    detail: Any = None


class BaseResponse(BaseModel, Generic[T]):
    """Universal response envelope used by every endpoint.

    Success:  ``{"success": true,  "data": <T>,   "error": null, "request_id": "..."}``
    Failure:  ``{"success": false, "data": null,   "error": {...}, "request_id": "..."}``
    """

    success: bool
    data: T | None = None
    error: ErrorDetail | None = None
    request_id: str = ""


class TokenData(BaseModel):
    """Internal data payload used by AuthService when generating a token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class OAuth2TokenResponse(BaseModel):
    """Flat response for POST /token — OAuth2-standard structure.

    Swagger UI requires ``access_token`` and ``token_type`` at the TOP LEVEL
    of the response to auto-populate the Authorization header.  Other endpoints
    continue to use the unified ``BaseResponse[T]`` envelope.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenResponse(BaseResponse[TokenData]):
    """Unified response for POST /token (kept for programmatic consumers)."""


class VerifyData(BaseModel):
    """Data payload returned by GET /api/v1/auth/verify."""

    account: str
    scopes: list[str]
    valid: bool = True


class VerifyResponse(BaseResponse[VerifyData]):
    """Unified response for GET /api/v1/auth/verify."""


class HashPasswordData(BaseModel):
    """Data payload returned by POST /api/v1/auth/hash-password."""

    hashed_password: str


class HashPasswordResponse(BaseResponse[HashPasswordData]):
    """Unified response for POST /api/v1/auth/hash-password."""


class MyScopesData(BaseModel):
    """Data payload returned by GET /api/v1/auth/my-scopes."""

    account: str
    scopes: list[str]


class MyScopesResponse(BaseResponse[MyScopesData]):
    """Unified response for GET /api/v1/auth/my-scopes."""
