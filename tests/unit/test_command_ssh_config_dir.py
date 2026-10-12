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
from app.core.exceptions import BaseAppException, SSHConfigNotFoundException

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

    cfg = SshSupport()._load_ssh_config("default")
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

    cfg = SshSupport()._load_ssh_config("default")
    assert cfg.auth_method == "key"


def test_missing_fallback_config_is_a_server_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(tmp_path))
    with pytest.raises(BaseAppException) as exc:
        SshSupport()._load_ssh_config("default")
    assert exc.value.http_status == 500


def test_named_config_is_loaded_literally_without_fallback(monkeypatch, tmp_path):
    """An explicit name expresses intent: silently authenticating with the
    Fallback SSH config would hide a missing secret (ADR 0002)."""
    _write_default(tmp_path)
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(tmp_path))
    with pytest.raises(SSHConfigNotFoundException) as exc:
        SshSupport()._load_ssh_config("nope")
    assert exc.value.http_status == 400
    assert exc.value.detail == {"ssh_config": "nope"}


# ── Auto-selection (ADR 0002) ────────────────────────────────────────────────


def _write(directory, name):
    (directory / f"SSH-{name}.json").write_text(json.dumps(_CFG))


@pytest.fixture
def ssh_dir(monkeypatch, tmp_path):
    _write_default(tmp_path)
    monkeypatch.setattr(ssh_mod.settings, "SSH_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_explicit_name_is_used_as_is_even_with_a_bastion(ssh_dir):
    assert SshSupport().select_ssh_config("cluster1", "b1") == "cluster1"


def test_default_without_a_bastion_is_the_fallback(ssh_dir):
    assert SshSupport().select_ssh_config("default", None) == "default"


def test_default_with_a_provisioned_bastion_selects_the_bastion(ssh_dir):
    _write(ssh_dir, "b1")
    assert SshSupport().select_ssh_config("default", "b1") == "b1"


def test_default_with_an_unprovisioned_bastion_falls_back_and_warns(ssh_dir, caplog):
    with caplog.at_level("WARNING"):
        assert SshSupport().select_ssh_config("default", "b9") == "default"
    assert "b9" in caplog.text


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "b 1"])
def test_unsafe_bastion_name_is_an_upstream_data_error(ssh_dir, bad):
    with pytest.raises(BaseAppException) as exc:
        SshSupport().select_ssh_config("default", bad)
    assert exc.value.http_status == 500
