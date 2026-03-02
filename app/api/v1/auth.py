"""
app/api/v1/auth.py

Auth endpoints (v1).

All responses use the unified BaseResponse[T] envelope.
Business logic lives in AuthService — routers only wire HTTP <-> service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.core.dependencies import get_current_user
from app.domain.models import (
    HashPasswordRequest,
    HashPasswordResponse,
    MyScopesResponse,
    TokenResponse,
    VerifyResponse,
)
from app.domain.models import User
from app.services.auth_service import AuthService
from app.repositories.json_user_repository import JsonUserRepository
from app.core.config import get_settings

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _get_auth_service() -> AuthService:
    """Build AuthService with the configured repository."""
    settings = get_settings()
    repo = JsonUserRepository(settings.USERS_JSON_PATH)
    return AuthService(repo)


# ── POST /token ───────────────────────────────────────────────────────────────

@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Login — obtain a JWT access token",
    description=(
        "Accepts standard OAuth2 `application/x-www-form-urlencoded` credentials. "
        "Returns a signed JWT token with the account's scopes embedded."
    ),
)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    svc: AuthService = Depends(_get_auth_service),
) -> TokenResponse:
    request_id: str = getattr(request.state, "request_id", "")
    user = await svc.authenticate(form_data.username, form_data.password)
    token_data = await svc.generate_token(user)
    return TokenResponse(success=True, data=token_data, request_id=request_id)


# ── GET /auth/verify ──────────────────────────────────────────────────────────

@router.get(
    "/verify",
    response_model=VerifyResponse,
    summary="Verify token validity",
    description="Returns the account name and scopes embedded in the provided JWT.",
)
async def verify(
    request: Request,
    current_user: User = Depends(get_current_user()),
    svc: AuthService = Depends(_get_auth_service),
) -> VerifyResponse:
    request_id: str = getattr(request.state, "request_id", "")
    verify_data = await svc.verify_user(current_user)
    return VerifyResponse(success=True, data=verify_data, request_id=request_id)


# ── POST /auth/hash-password ──────────────────────────────────────────────────

@router.post(
    "/hash-password",
    response_model=HashPasswordResponse,
    summary="Hash a plain-text password",
    description=(
        "Returns a bcrypt hash of the provided password. "
        "Paste the result into `data/users.json` under `hashed_password` to change a password. "
        "No authentication required."
    ),
)
async def hash_password_endpoint(
    request: Request,
    body: HashPasswordRequest,
    svc: AuthService = Depends(_get_auth_service),
) -> HashPasswordResponse:
    request_id: str = getattr(request.state, "request_id", "")
    hash_data = await svc.hash_plain_password(body.password)
    return HashPasswordResponse(success=True, data=hash_data, request_id=request_id)


# ── GET /auth/my-scopes ───────────────────────────────────────────────────────

@router.get(
    "/my-scopes",
    response_model=MyScopesResponse,
    summary="Inspect current token scopes",
    description="Returns the scopes granted to the authenticated caller's token.",
)
async def my_scopes(
    request: Request,
    current_user: User = Depends(get_current_user()),
    svc: AuthService = Depends(_get_auth_service),
) -> MyScopesResponse:
    request_id: str = getattr(request.state, "request_id", "")
    scopes_data = await svc.get_my_scopes(current_user)
    return MyScopesResponse(success=True, data=scopes_data, request_id=request_id)
