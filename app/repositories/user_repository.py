"""
app/repositories/user_repository.py

Abstract interface for user persistence.

Dependency Inversion Principle:
  - Higher layers (services) depend on this abstract interface.
  - Lower layers (JsonUserRepository, SqlUserRepository, …) implement it.
  - Swapping storage backends requires zero changes in service or router code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.models import UserInDB


class UserRepository(ABC):
    """Abstract contract for user data access."""

    @abstractmethod
    async def get_by_account(self, account: str) -> UserInDB | None:
        """Return the stored user record for *account*, or ``None`` if not found."""

    @abstractmethod
    async def list_accounts(self) -> list[str]:
        """Return a list of all known account names."""
