"""Unit tests for InventoryService."""
from __future__ import annotations

from app.repositories.inventory_repository import NodeBastionResolution


def test_node_bastion_resolution_import():
    """Test that NodeBastionResolution can be imported."""
    assert NodeBastionResolution is not None
