import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ansible" / "run-ansible.sh"


def _run(tmp_path, *extra):
    return subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True,
        env={**os.environ, "DRYRUN": "1"},
    )


def test_bad_run_id_rejected(tmp_path):
    res = _run(tmp_path, "--run-id", "../evil")
    assert res.returncode == 2
    assert "run-id" in (res.stderr + res.stdout).lower()


def test_run_id_sets_log_filename(tmp_path):
    res = _run(tmp_path, "--run-id", "abc-123")
    assert res.returncode == 0, res.stderr
    assert str(tmp_path / "abc-123.log") in res.stdout


def test_bad_retention_rejected(tmp_path):
    res = _run(tmp_path, "--run-id", "ok", "--log-retention-days", "abc")
    assert res.returncode == 2
    assert "retention" in (res.stderr + res.stdout).lower()


def test_self_cleaning_prunes_old_logs(tmp_path):
    old = tmp_path / "old.log"
    fresh = tmp_path / "fresh.log"
    old.write_text("x")
    fresh.write_text("y")
    # Backdate old.log to 5 days ago (default retention is 3 → it must go).
    five_days_ago = time.time() - 5 * 86400
    os.utime(old, (five_days_ago, five_days_ago))

    res = _run(tmp_path, "--run-id", "run9")
    assert res.returncode == 0, res.stderr
    assert not old.exists(), "5-day-old log should be pruned at default retention 3"
    assert fresh.exists(), "fresh log must be kept"


def test_retention_zero_disables_cleanup(tmp_path):
    old = tmp_path / "old.log"
    old.write_text("x")
    five_days_ago = time.time() - 5 * 86400
    os.utime(old, (five_days_ago, five_days_ago))

    res = _run(tmp_path, "--run-id", "run9", "--log-retention-days", "0")
    assert res.returncode == 0, res.stderr
    assert old.exists(), "retention 0 must disable cleanup"


def test_playbook_dir_mounts_local_dir(tmp_path):
    # A local playbook dir is bind-mounted over the image's /playbooks so
    # newly-added playbooks (e.g. clock.yml) are visible in the container.
    pdir = tmp_path / "playbooks"
    pdir.mkdir()
    (pdir / "clock.yml").write_text("---\n")
    res = _run(tmp_path, "--run-id", "ok", "--playbook-dir", str(pdir))
    assert res.returncode == 0, res.stderr
    out = res.stdout + res.stderr
    assert str(pdir) in out and "/playbooks" in out


def test_playbook_dir_missing_rejected(tmp_path):
    res = _run(tmp_path, "--run-id", "ok",
               "--playbook-dir", str(tmp_path / "does-not-exist"))
    assert res.returncode == 2
    assert "playbook-dir" in (res.stderr + res.stdout).lower()


def test_no_playbook_dir_keeps_image_default(tmp_path):
    # Without --playbook-dir the script must not emit a host playbook mount;
    # the image's baked /playbooks is used.
    res = _run(tmp_path, "--run-id", "ok")
    assert res.returncode == 0, res.stderr
    assert "playbook mount" not in (res.stdout + res.stderr).lower()
