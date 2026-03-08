"""
app/main.py

FastAPI application entry point.

Responsibilities:
  - Lifespan hook (startup / graceful shutdown)
  - Middleware registration (RequestId)
  - Exception handler registration
  - Router mounting
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

from app.api.router import api_router
from app.core.config import get_settings
from app.core.exceptions import (
    BaseAppException,
    app_exception_handler,
    unhandled_exception_handler,
)
from app.core.logging import RequestIdMiddleware, setup_logging
from app.domain.models import OAuth2TokenResponse
from app.repositories.json_user_repository import JsonUserRepository
from app.services.auth_service import AuthService

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager.

    Startup:  configure logging, validate config.
    Shutdown: emit a graceful-shutdown log so orchestrators know we're done.
    """
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    _logger.info(
        "Starting %s v%s | env=%s",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.APP_ENV,
    )

    yield  # ← application runs here

    _logger.info(
        "Shutting down %s — goodbye.", settings.APP_NAME
    )


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # ── Middleware ─────────────────────────────────────────────────────────────
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],   # Tighten per environment as needed
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Exception handlers ─────────────────────────────────────────────────────
    app.add_exception_handler(BaseAppException, app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ── Health check ───────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"], summary="Health check")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    # ── Routes ─────────────────────────────────────────────────────────────────
    # Standard OAuth2 token endpoint at root /token.
    # Must return {access_token, token_type} at the TOP LEVEL so that
    # Swagger UI can extract the token and auto-fill Authorization headers.
    @app.post(
        "/token",
        response_model=OAuth2TokenResponse,
        tags=["auth"],
        summary="Login — obtain a JWT access token",
    )
    async def token_endpoint(
        form_data: OAuth2PasswordRequestForm = Depends(),
    ) -> OAuth2TokenResponse:
        svc = AuthService(JsonUserRepository(settings.USERS_JSON_PATH))
        user = await svc.authenticate(form_data.username, form_data.password)
        token_data = await svc.generate_token(user)
        return OAuth2TokenResponse(
            access_token=token_data.access_token,
            token_type=token_data.token_type,
            expires_in=token_data.expires_in,
        )

    app.include_router(api_router)

    return app


app = create_app()
