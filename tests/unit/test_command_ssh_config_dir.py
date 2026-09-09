"""SshSupport._load_ssh_config: which directory the SSH-*.json files load from.

SSH configs carry private keys, so they read from ``SSH_CONFIG_DIR`` — kept
separate from the command whitelists (``COMMAND_CONFIG_DIR``) so production can
point it at a Vault-backed mount. When ``SSH_CONFIG_DIR`` is unset it falls back
to ``COMMAND_CONFIG_DIR`` so single-directory deployments keep working.
"""

import json

import pytest

from app.services import command_ssh as ssh_mod
from app.services.command_ssh import SshSupport
from app.core.exceptions import BaseAppException

_CFG = {"auth_method": "key", "key_base64": "Zm9v"}


def _write_default(directory):
    (directory / "SSH-default.json").write_text(json.dumps(_CFG))


def test_uses_ssh_config_dir_when_set(monkeypatch, tmp_path):
    ssh_dir = tmp_path / "vault"
    ssh_dir.mkdir()
    _write_default(ssh_dir)
    # COMMAND_CONFIG_DIR points elsewhere (no SSH files) — must not be consulted.
    monkeypatch.setattr(ssh_mod.settings, "COMMAND_CONFIG_DIR", str(tmp_path / "cmd"))
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(ssh_dir))

    cfg = SshSupport()._load_ssh_config("some-cluster")
    assert cfg.auth_method == "key"


def test_target_specific_file_wins_over_default(monkeypatch, tmp_path):
    _write_default(tmp_path)
    (tmp_path / "SSH-cluster1.json").write_text(
        json.dumps({"auth_method": "key", "key_base64": "YmFy"})
    )
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(tmp_path))

    cfg = SshSupport()._load_ssh_config("cluster1")
    assert cfg.key_base64 == "YmFy"


def test_falls_back_to_command_config_dir_when_unset(monkeypatch, tmp_path):
    _write_default(tmp_path)
    # SSH_CONFIG_DIR empty → the loader must fall back to COMMAND_CONFIG_DIR.
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", "")
    monkeypatch.setattr(ssh_mod.settings, "COMMAND_CONFIG_DIR", str(tmp_path))

    cfg = SshSupport()._load_ssh_config("whatever")
    assert cfg.auth_method == "key"


def test_missing_config_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(tmp_path))
    with pytest.raises(BaseAppException):
        SshSupport()._load_ssh_config("nope")
