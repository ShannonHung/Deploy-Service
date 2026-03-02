"""
app/api/router.py

Top-level API router.

Route layout:
  POST /token                      → OAuth2 token endpoint (root-level for standard compliance)
  GET  /api/v1/auth/verify         → Verify token
  POST /api/v1/auth/hash-password  → Hash a password
  GET  /api/v1/auth/my-scopes      → Inspect token scopes
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router

# ── /api/v1 sub-router ────────────────────────────────────────────────────────
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(auth_router)  # mounts at /api/v1/auth/...

# ── Root router (aggregates everything) ───────────────────────────────────────
api_router = APIRouter()
api_router.include_router(v1_router)
