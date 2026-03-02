"""
app/core/dependencies.py

FastAPI dependency factory for scope-based access control.

Usage in routes:
    @router.get("/deploy")
    def deploy(user: User = Depends(get_current_user(["deploy_api"]))):
        ...
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import AuthException, ForbiddenException
from app.core.security import decode_access_token
from app.domain.models import User

# The tokenUrl must match your actual POST /token route path.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")


def get_current_user(required_scopes: list[str] | None = None) -> Callable:
    """Dependency factory that validates JWT and enforces scope requirements.

    Args:
        required_scopes: List of scope strings the token must contain ALL of.
                         Pass an empty list or None to only validate the token.

    Returns:
        A FastAPI dependency callable that resolves to the authenticated ``User``.

    Raises:
        AuthException:   Token is missing, invalid, or expired.
        ForbiddenException: Token is valid but lacks a required scope.
    """
    required: list[str] = required_scopes or []

    async def _dependency(token: str = Depends(oauth2_scheme)) -> User:
        payload = decode_access_token(token)  # raises AuthException on failure

        account: str | None = payload.get("sub")
        scopes: list[str] = payload.get("scopes", [])

        if not account:
            raise AuthException(
                "Token is missing subject claim.",
                source_function="get_current_user._dependency",
            )

        missing = [s for s in required if s not in scopes]
        if missing:
            raise ForbiddenException(
                f"Token is missing required scopes: {missing}",
                source_function="get_current_user._dependency",
                detail={"required": required, "missing": missing},
            )

        return User(account=account, scopes=scopes)

    return _dependency
