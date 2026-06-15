"""
tests/e2e/conftest.py

E2E-only fixtures.  These tests require live infrastructure (Redis + docker
SSH nodes) and are only run when RUN_E2E=1 is set.

SSH config files that contain real CA-signed keys live in data/ and must
never be committed to tests/fixtures/.  The fixture below bridges the gap
by copying each required SSH-<target>.json into tests/fixtures/ for the
duration of the session, then removing it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_DATA_DIR = Path(__file__).parent.parent.parent / "data"

_E2E_SSH_CONFIGS = ["cluster1"]


@pytest.fixture(scope="session", autouse=True)
def _e2e_ssh_fixtures():
    """Copy data/SSH-<target>.json into tests/fixtures/ for the e2e session."""
    copied: list[Path] = []
    for target in _E2E_SSH_CONFIGS:
        src = _DATA_DIR / f"SSH-{target}.json"
        dst = _FIXTURES_DIR / f"SSH-{target}.json"
        if src.exists():
            shutil.copy2(src, dst)
            copied.append(dst)
    yield
    for dst in copied:
        dst.unlink(missing_ok=True)
