# run-ansible.sh Logging, Debug/Dry-run, image-tag, and Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `deploy-service/ansible/run-ansible.sh` into readable functions and add observable logging, a `--debug` idle-container mode, a `--dry-run` print-only mode, and an `--image-tag` flag — without changing any behavior the existing integration test guarantees.

**Architecture:** The single-pass bash script becomes a set of focused functions orchestrated by `main()`. A new `MODE` variable (`normal` | `debug` | `dry-run`) selects the terminal action. All user values stay as discrete args (no `eval`, no shell-string building). The existing `tests/integration/test_run_ansible_script.py` is the behavioral contract and must stay green throughout; new features are added TDD-style with the same fake-git+fake-docker harness.

**Tech Stack:** Bash (`set -euo pipefail`), Docker (DooD), git, pytest + subprocess integration tests.

## Global Constraints

- File under change: `deploy-service/ansible/run-ansible.sh` only (no deploy-service code).
- Anti-injection is load-bearing: pass user values as discrete array args; never `eval`; never build a shell string from user input.
- Preserve the deploy-service contract exactly: `=== EXIT <code> ===` log marker, atomic `<run_id>.exit` sidecar (only when `--run-id` set), re-exit with the real ansible/docker exit code, captured via `${PIPESTATUS[0]}` inside a `set +e` / `set -e` window.
- Preserve the `DRYRUN=1` env hook: early-exit BEFORE clone/docker, printing `DRYRUN log file: <path>`. This is distinct from the new `--dry-run` flag.
- Preserve `--run-id` validation `^[A-Za-z0-9_-]+$`, `--log-retention-days` non-negative-integer validation + pruning (0 disables), and inventory path-traversal rejection.
- Default image repo: `shannonhung/ansible-runner`. Default tag: `latest`.
- Mutual exclusion: `--debug` + `--dry-run` → exit 2; `--image` + `--image-tag` → exit 2.
- Debug container name: `ansible-debug-<run_id>` when `--run-id` set, else `ansible-debug-<random>`.
- All tests run from `deploy-service/`: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`.

---

### Task 1: Refactor into functions (behavior-preserving)

Rewrite the script into functions orchestrated by `main()`, keeping behavior byte-for-byte observable to the existing test suite. No new flags yet. This task is gated entirely by the existing tests.

**Files:**
- Modify (full rewrite): `deploy-service/ansible/run-ansible.sh`
- Test (existing, unchanged): `deploy-service/tests/integration/test_run_ansible_script.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (functions later tasks extend):
  - `usage()` — prints help.
  - `parse_args "$@"` — sets globals: `PLAYBOOK INVENTORY INVENTORY_REF TAGS LIMIT EXTRA_VARS IMAGE PULL LOG_DIR RUN_ID LOG_RETENTION_DAYS SSH_KEY` (plus `MODE` added in later tasks).
  - `resolve_log_file()` — validates `RUN_ID`/`LOG_RETENTION_DAYS`, sets `LOG_FILE`, honors `DRYRUN=1` early-exit, runs `mkdir -p "$LOG_DIR"` and pruning.
  - `clone_inventory()` — sets `SCRIPT_DIR CLONE_PARENT CLONE_DIR`, arms `trap cleanup EXIT`, clones, validates traversal + file existence.
  - `build_cmd_args()` — sets array `CMD_ARGS=(ansible-playbook -i /inventory/$INVENTORY /playbooks/$PLAYBOOK ...)`.
  - `run_normal()` — validates ssh key, `docker run --rm ... | tee`, captures `${PIPESTATUS[0]}`, writes marker + sidecar, re-exits real code.
  - `cleanup()` — `rm -rf "$CLONE_DIR"`.
  - `main "$@"` — calls the above in order.

- [ ] **Step 1: Run the existing suite to capture the green baseline**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: all tests PASS (this is the contract we must keep).

- [ ] **Step 2: Rewrite the script into functions**

Replace the entire contents of `deploy-service/ansible/run-ansible.sh` with the functional structure below. This preserves all current logic; it only reorganizes it. (New flags are added in later tasks — do not add them here.)

```bash
#!/usr/bin/env bash
#
# run-ansible.sh — launch the ansible runner image to execute a playbook.
#
# clone inventory -> docker run ansible image -> ansible-playbook -> SSH -> nodes
#
# Always-latest: the inventory repo is cloned FRESH every run and deleted on exit
# (trap); the runner image is `docker pull`-ed unless --no-pull. All user values
# are passed as DISCRETE ARGS (never interpolated into a shell string) — this is
# the load-bearing anti-injection guarantee. Do NOT add `eval` here.

set -euo pipefail

# ── Fixed config (not user-overridable by design) ────────────────────────────
# INVENTORY_REPO env var can override the repo for local testing (e.g. file://).
INVENTORY_REPO="${INVENTORY_REPO:-https://gitlab.com/ShannonHung/my-ansible-inventory.git}"
IMAGE="shannonhung/ansible-runner:latest"

# ── Defaults ─────────────────────────────────────────────────────────────────
PLAYBOOK=""
INVENTORY=""               # path RELATIVE to the inventory repo root
INVENTORY_REF="main"       # branch/tag of the inventory repo to clone
TAGS=""
LIMIT=""
EXTRA_VARS=""
PULL=1                     # docker pull before run; --no-pull disables
LOG_DIR="$(pwd)/logs"
RUN_ID=""                  # per-run id from deploy-service; log is <run_id>.log
LOG_RETENTION_DAYS=3       # prune <log-dir>/*.log older than this many days
SSH_KEY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/ssh_keys/client_key"

usage() {
  cat <<'EOF'
Usage: run-ansible.sh --playbook <file> --inventory <repo-relative-path> [options]

Required:
  --playbook <file>       Playbook filename under playbooks/ (e.g. ping.yml)
  --inventory <path>      Inventory path RELATIVE to the inventory repo root
                          (e.g. taipei/multinode.ini)

Options:
  --inventory-ref <ref>   Branch/tag of the inventory repo to clone (default: main)
  --tags <tags>           Comma-separated ansible --tags
  --limit <pattern>       ansible --limit host/group pattern
  --extra-vars <k=v ...>  ansible --extra-vars string
  --image <name>          Runner image full name (default: shannonhung/ansible-runner:latest)
  --no-pull               Skip `docker pull` (use a locally-built image)
  --log-dir <path>        Host dir to mount for logs (default: ./logs)
  --run-id <id>           Per-run id; log is <log-dir>/<id>.log (^[A-Za-z0-9_-]+$)
  --log-retention-days <n>  Delete <log-dir>/*.log older than n days (default: 3; 0 disables)
  --ssh-key <path>        SSH private key to mount (default: ../data/ssh_keys/client_key)
  -h, --help              Show this help

The inventory repo (fixed) is cloned fresh each run and removed afterward:
  https://gitlab.com/ShannonHung/my-ansible-inventory.git

Example:
  ./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini --limit node1
EOF
}

# ── Arg parsing ──────────────────────────────────────────────────────────────
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --playbook)            PLAYBOOK="$2"; shift 2 ;;
      --inventory)           INVENTORY="$2"; shift 2 ;;
      --inventory-ref)       INVENTORY_REF="$2"; shift 2 ;;
      --tags)                TAGS="$2"; shift 2 ;;
      --limit)               LIMIT="$2"; shift 2 ;;
      --extra-vars)          EXTRA_VARS="$2"; shift 2 ;;
      --image)               IMAGE="$2"; shift 2 ;;
      --no-pull)             PULL=0; shift ;;
      --log-dir)             LOG_DIR="$2"; shift 2 ;;
      --run-id)              RUN_ID="$2"; shift 2 ;;
      --log-retention-days)  LOG_RETENTION_DAYS="$2"; shift 2 ;;
      --ssh-key)             SSH_KEY="$2"; shift 2 ;;
      -h|--help)             usage; exit 0 ;;
      *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
  done

  if [[ -z "$PLAYBOOK" || -z "$INVENTORY" ]]; then
    echo "Error: --playbook and --inventory are required." >&2
    usage
    exit 2
  fi
}

# ── Per-run log file + self-cleaning ─────────────────────────────────────────
resolve_log_file() {
  # RUN_ID becomes a filename, so validate it strictly. Empty RUN_ID keeps the
  # legacy single-file behaviour (run.log) for standalone use.
  if [[ -n "$RUN_ID" ]]; then
    if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "Error: --run-id must match ^[A-Za-z0-9_-]+$" >&2
      exit 2
    fi
    LOG_FILE="$LOG_DIR/$RUN_ID.log"
  else
    LOG_FILE="$LOG_DIR/run.log"
  fi

  if [[ ! "$LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    echo "Error: --log-retention-days must be a non-negative integer." >&2
    exit 2
  fi

  mkdir -p "$LOG_DIR"

  # Prune old logs BEFORE work so a killed run never skips cleanup. Guarded so an
  # empty LOG_DIR can't widen the delete scope. Only files older than the window
  # go — never the in-flight <run_id>.log. 0 disables.
  if [[ "$LOG_RETENTION_DAYS" -gt 0 && -n "$LOG_DIR" && -d "$LOG_DIR" ]]; then
    find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime "+$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
  fi

  # Test/inspection hook: print the resolved log path and exit before any docker
  # or git work. Used by the script's unit test (no network/docker required).
  if [[ "${DRYRUN:-0}" == "1" ]]; then
    echo "DRYRUN log file: $LOG_FILE"
    exit 0
  fi
}

# ── Fresh inventory clone (deleted on exit, always latest) ───────────────────
cleanup() {
  rm -rf "$CLONE_DIR"
}

clone_inventory() {
  # DooD: the clone dir is bind-mounted into the ansible container, and -v
  # resolves on the HOST daemon. So clone beside this script (host-consistent),
  # NOT control_node's private /tmp. Override with CLONE_PARENT if needed.
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CLONE_PARENT="${CLONE_PARENT:-$SCRIPT_DIR/.run-tmp}"
  mkdir -p "$CLONE_PARENT"
  CLONE_DIR="$(mktemp -d "$CLONE_PARENT/ansible-inventory.XXXXXX")"
  trap cleanup EXIT

  echo ">> Cloning inventory (ref: $INVENTORY_REF) from $INVENTORY_REPO into $CLONE_DIR"
  git clone --depth 1 --branch "$INVENTORY_REF" "$INVENTORY_REPO" "$CLONE_DIR"

  # Reject path traversal so the relative path can't escape the cloned repo.
  case "$INVENTORY" in
    /*|*..*) echo "Error: --inventory must be a relative path inside the repo." >&2; exit 2 ;;
  esac
  if [[ ! -f "$CLONE_DIR/$INVENTORY" ]]; then
    echo "Error: inventory file not found in repo: $INVENTORY" >&2
    echo "Available inventory files:" >&2
    find "$CLONE_DIR" -name '*.ini' -o -name '*.yml' -path '*inventor*' 2>/dev/null | sed "s#$CLONE_DIR/#  #" >&2 || true
    exit 2
  fi
}

# ── Build the ansible command (discrete args, no eval) ───────────────────────
build_cmd_args() {
  CMD_ARGS=(ansible-playbook -i "/inventory/$INVENTORY" "/playbooks/$PLAYBOOK")
  [[ -n "$TAGS"       ]] && CMD_ARGS+=(--tags "$TAGS")
  [[ -n "$LIMIT"      ]] && CMD_ARGS+=(--limit "$LIMIT")
  [[ -n "$EXTRA_VARS" ]] && CMD_ARGS+=(--extra-vars "$EXTRA_VARS")
  return 0
}

# ── Normal run: docker run + tee + EXIT marker + sidecar + re-exit ───────────
run_normal() {
  if [[ "$PULL" -eq 1 ]]; then
    echo ">> Pulling latest image: $IMAGE"
    docker pull "$IMAGE"
  fi

  echo ">> Running: ${CMD_ARGS[*]}"
  echo ">> Logs:    $LOG_FILE (tee'd from stdout)"

  # SSH key is only consumed by docker run; validate here (after DRYRUN/arg/
  # inventory checks) so dry-run and unit tests don't require a real key.
  # SKIP_SSH_KEY_CHECK=1 lets fake-docker tests run without one.
  if [[ "${SKIP_SSH_KEY_CHECK:-0}" != "1" && ! -f "$SSH_KEY" ]]; then
    echo "Error: ssh key not found: $SSH_KEY" >&2
    exit 2
  fi

  # set -e would abort before we record a non-zero exit, so capture via
  # ${PIPESTATUS[0]} (the docker side of the pipe, NOT tee's) and re-exit it.
  set +e
  docker run --rm \
    --add-host host.docker.internal:host-gateway \
    -v "$CLONE_DIR":/inventory:ro \
    -v "$SSH_KEY":/root/.ssh/id_key:ro \
    -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
    -e ANSIBLE_COLLECTIONS_PATH=/collections \
    "$IMAGE" \
    "${CMD_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  RUN_EXIT="${PIPESTATUS[0]}"
  set -e

  echo "=== EXIT $RUN_EXIT ===" >> "$LOG_FILE"

  if [[ -n "$RUN_ID" ]]; then
    EXIT_FILE="$LOG_DIR/$RUN_ID.exit"
    printf '%s\n' "$RUN_EXIT" > "$EXIT_FILE.tmp" && mv -f "$EXIT_FILE.tmp" "$EXIT_FILE"
  fi

  exit "$RUN_EXIT"
}

main() {
  parse_args "$@"
  resolve_log_file
  clone_inventory
  build_cmd_args
  run_normal
}

main "$@"
```

- [ ] **Step 3: Run the existing suite to verify behavior is unchanged**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: all tests PASS (same as the Step 1 baseline).

- [ ] **Step 4: Commit**

```bash
cd deploy-service
git add ansible/run-ansible.sh
git commit -m "refactor(ansible): split run-ansible.sh into functions (behavior-preserving)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Run Summary + full docker-run logging

Add `print_summary()` (centered block) and `print_docker_run()` (full docker command), and call them from `run_normal()`. Add the `>> Inventory resolved:` line to `clone_inventory()`.

**Files:**
- Modify: `deploy-service/ansible/run-ansible.sh`
- Test: `deploy-service/tests/integration/test_run_ansible_script.py`

**Interfaces:**
- Consumes: globals from Task 1 (`INVENTORY_REPO INVENTORY_REF CLONE_DIR INVENTORY PLAYBOOK IMAGE SSH_KEY LOG_FILE CMD_ARGS`).
- Produces:
  - `print_summary()` — prints the RUN SUMMARY block.
  - `print_docker_run()` — prints the full `docker run ...` command as text.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_run_ansible_script.py`:

```python
def test_summary_and_docker_run_logged(tmp_path):
    res = _run_with_fake_docker(tmp_path, 0, "--run-id", "sum1", "--limit", "node1")
    out = res.stdout
    assert res.returncode == 0, res.stderr
    assert "RUN SUMMARY" in out
    assert "Inventory repo" in out
    assert "Inventory resolved: /inventory/taipei/multinode.ini" in out
    # Full docker run command (mounts/env/add-host), not just the ansible part.
    assert "docker run" in out
    assert "host.docker.internal:host-gateway" in out
    assert "/inventory:ro" in out
    assert "ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py::test_summary_and_docker_run_logged -v`
Expected: FAIL — "RUN SUMMARY" not in output.

- [ ] **Step 3: Implement the logging functions**

In `clone_inventory()`, after the inventory-existence check passes (just before the closing `}`), add:

```bash
  echo ">> Inventory resolved: /inventory/$INVENTORY"
```

Add these two functions after `build_cmd_args()`:

```bash
# ── Logging: human-readable run summary + the exact docker command ───────────
print_summary() {
  cat <<EOF
══════════════════ RUN SUMMARY ══════════════════
  Inventory repo : $INVENTORY_REPO
  Inventory ref  : $INVENTORY_REF
  Clone dir      : $CLONE_DIR
  Inventory file : /inventory/$INVENTORY
  Playbook       : /playbooks/$PLAYBOOK
  Image          : $IMAGE
  SSH key        : $SSH_KEY
  Ansible cmd    : ${CMD_ARGS[*]}
  Log file       : $LOG_FILE
══════════════════════════════════════════════════
EOF
}

print_docker_run() {
  cat <<EOF
>> docker run command:
   docker run --rm \\
     --add-host host.docker.internal:host-gateway \\
     -v $CLONE_DIR:/inventory:ro \\
     -v $SSH_KEY:/root/.ssh/id_key:ro \\
     -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \\
     -e ANSIBLE_COLLECTIONS_PATH=/collections \\
     $IMAGE \\
     ${CMD_ARGS[*]}
EOF
}
```

In `run_normal()`, add the two calls right after the `if [[ "$PULL" -eq 1 ]] ... fi` block and before `echo ">> Running:..."`:

```bash
  print_summary
  print_docker_run
```

- [ ] **Step 4: Run the new test and the full suite**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: all PASS, including `test_summary_and_docker_run_logged`.

- [ ] **Step 5: Commit**

```bash
cd deploy-service
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(ansible): add run summary + full docker-run logging

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `--image-tag` flag (+ mutual exclusion with `--image`)

Add `--image-tag <tag>` → `shannonhung/ansible-runner:<tag>`, mutually exclusive with `--image`.

**Files:**
- Modify: `deploy-service/ansible/run-ansible.sh`
- Test: `deploy-service/tests/integration/test_run_ansible_script.py`

**Interfaces:**
- Consumes: `IMAGE` default from Task 1.
- Produces: globals `IMAGE_TAG` and `IMAGE_SET`; `resolve_image()` finalizes `IMAGE`.

- [ ] **Step 1: Write the failing tests**

Add to the test file:

```python
def test_image_tag_sets_image(tmp_path):
    res = _run_with_fake_docker(tmp_path, 0, "--run-id", "it1", "--image-tag", "v1.2")
    assert res.returncode == 0, res.stderr
    assert "shannonhung/ansible-runner:v1.2" in res.stdout

def test_image_and_image_tag_mutually_exclusive(tmp_path):
    res = _run(tmp_path, "--image", "foo/bar:1", "--image-tag", "v1.2")
    assert res.returncode == 2
    assert "image" in (res.stderr + res.stdout).lower()
```

(`_run` uses `DRYRUN=1`, so the mutual-exclusion check must happen in `parse_args`/before the `DRYRUN` early-exit. We put the check in `parse_args`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py::test_image_tag_sets_image tests/integration/test_run_ansible_script.py::test_image_and_image_tag_mutually_exclusive -v`
Expected: FAIL — `--image-tag` is an unknown argument (exit 2 with "Unknown argument").

- [ ] **Step 3: Implement**

In the Defaults section add:

```bash
IMAGE_TAG=""               # --image-tag <tag>: shannonhung/ansible-runner:<tag>
IMAGE_SET=0                # 1 if --image was given (for mutual-exclusion check)
```

In `parse_args`, change the `--image` case and add `--image-tag`:

```bash
      --image)               IMAGE="$2"; IMAGE_SET=1; shift 2 ;;
      --image-tag)           IMAGE_TAG="$2"; shift 2 ;;
```

Add to `usage()` Options (after the `--image` line):

```
  --image-tag <tag>       Use shannonhung/ansible-runner:<tag> (mutually exclusive with --image)
```

At the end of `parse_args` (after the required-args check), add the mutual-exclusion + resolution:

```bash
  if [[ "$IMAGE_SET" -eq 1 && -n "$IMAGE_TAG" ]]; then
    echo "Error: --image and --image-tag are mutually exclusive." >&2
    exit 2
  fi
  if [[ -n "$IMAGE_TAG" ]]; then
    IMAGE="shannonhung/ansible-runner:$IMAGE_TAG"
  fi
```

- [ ] **Step 4: Run the new tests and the full suite**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd deploy-service
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(ansible): add --image-tag flag (mutually exclusive with --image)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `--dry-run` mode

Add `MODE` selection and a `run_dry_run()` that clones (then deletes via trap), prints summary + docker run/ansible commands, and exits 0 without pulling or running docker.

**Files:**
- Modify: `deploy-service/ansible/run-ansible.sh`
- Test: `deploy-service/tests/integration/test_run_ansible_script.py`

**Interfaces:**
- Consumes: `print_summary`, `print_docker_run` (Task 2); `clone_inventory`, `build_cmd_args` (Task 1).
- Produces: global `MODE` (`normal`|`debug`|`dry-run`); `run_dry_run()`.

- [ ] **Step 1: Write the failing test**

The fake docker in `_run_with_fake_docker` exits 0; to prove docker was NOT run, use a fake docker that would FAIL the test if invoked. Add a helper + test:

```python
def _run_with_failing_docker(tmp_path, *extra):
    """Fake git that creates the inventory, plus a fake docker that exits 99 and
    writes a sentinel file — so any docker invocation is detectable."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{tmp_path}/docker_was_called"\n'
        "exit 99\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1"}
    return subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True, env=env,
    )


def test_dry_run_prints_but_does_not_run_docker(tmp_path):
    res = _run_with_failing_docker(tmp_path, "--dry-run", "--run-id", "dr1")
    assert res.returncode == 0, res.stderr
    assert "RUN SUMMARY" in res.stdout
    assert "docker run" in res.stdout            # printed as text
    assert not (tmp_path / "docker_was_called").exists()  # never executed
    assert not (tmp_path / "dr1.exit").exists()   # nothing ran → no sidecar
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py::test_dry_run_prints_but_does_not_run_docker -v`
Expected: FAIL — `--dry-run` is an unknown argument (exit 2).

- [ ] **Step 3: Implement**

In the Defaults section add:

```bash
MODE="normal"              # normal | debug | dry-run
```

In `parse_args`, add the case (before the `*)` catch-all):

```bash
      --dry-run)             MODE="dry-run"; shift ;;
```

Add to `usage()` Options:

```
  --dry-run               Clone inventory + print summary/commands; do NOT pull or run docker
```

Add `run_dry_run()` after `print_docker_run()`:

```bash
# ── Dry-run: clone + print everything, but never pull or run docker ──────────
# Distinct from DRYRUN=1 (which exits before clone). The clone dir is still
# removed by the EXIT trap armed in clone_inventory.
run_dry_run() {
  print_summary
  print_docker_run
  echo ">> --dry-run: skipping docker pull and docker run."
  exit 0
}
```

Change `main()` to dispatch on `MODE`:

```bash
main() {
  parse_args "$@"
  resolve_log_file
  clone_inventory
  build_cmd_args
  case "$MODE" in
    dry-run) run_dry_run ;;
    *)       run_normal ;;
  esac
}
```

- [ ] **Step 4: Run the new test and the full suite**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd deploy-service
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(ansible): add --dry-run mode (clone + print, no docker)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `-d/--debug` mode

Add `run_debug()` that starts an idle container (`sleep infinity`, no `--rm`), keeps the clone dir (disarms the trap), prints exec/manual-playbook/cleanup guidance, and exits 0. Enforce `--debug`/`--dry-run` mutual exclusion.

**Files:**
- Modify: `deploy-service/ansible/run-ansible.sh`
- Test: `deploy-service/tests/integration/test_run_ansible_script.py`

**Interfaces:**
- Consumes: `print_summary` (Task 2), `clone_inventory`/`build_cmd_args` (Task 1), `MODE` (Task 4), `IMAGE CLONE_DIR SSH_KEY RUN_ID INVENTORY PLAYBOOK TAGS LIMIT`.
- Produces: `run_debug()`; `DEBUG_CONTAINER` name.

- [ ] **Step 1: Write the failing tests**

```python
def test_debug_starts_idle_container_and_keeps_clone(tmp_path):
    # Fake docker records its argv so we can assert `run -d ... sleep infinity`.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{tmp_path}/docker_argv"\n'
        "exit 0\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1",
           # Keep the clone dir beside a known, inspectable parent.
           "CLONE_PARENT": str(tmp_path / "clones")}
    res = subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path),
         "--debug", "--run-id", "dbg1"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    argv = (tmp_path / "docker_argv").read_text()
    assert "run -d" in argv
    assert "sleep infinity" in argv
    assert "ansible-debug-dbg1" in argv
    # Guidance printed
    assert "docker exec -it ansible-debug-dbg1 bash" in res.stdout
    assert "ansible-playbook -i /inventory/taipei/multinode.ini" in res.stdout
    assert "docker rm -f ansible-debug-dbg1" in res.stdout
    # No sidecar; clone dir kept (not removed by trap)
    assert not (tmp_path / "dbg1.exit").exists()
    clones = list((tmp_path / "clones").glob("ansible-inventory.*"))
    assert clones, "debug mode must keep the clone dir"


def test_debug_and_dry_run_mutually_exclusive(tmp_path):
    res = _run(tmp_path, "--debug", "--dry-run")
    assert res.returncode == 2
    assert "debug" in (res.stderr + res.stdout).lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py::test_debug_starts_idle_container_and_keeps_clone tests/integration/test_run_ansible_script.py::test_debug_and_dry_run_mutually_exclusive -v`
Expected: FAIL — `--debug` is an unknown argument (exit 2).

- [ ] **Step 3: Implement**

In `parse_args`, add the case (before `*)`):

```bash
      -d|--debug)            MODE="debug"; shift ;;
```

At the end of `parse_args`, add the mutual-exclusion check (alongside the image one). Note: with the single `MODE` variable, `--debug --dry-run` means the later flag wins silently — to make it an explicit error, track both. Replace the `--dry-run` and `--debug` cases to set independent flags and resolve `MODE` at the end:

Add to Defaults:

```bash
WANT_DEBUG=0
WANT_DRY_RUN=0
```

Change the two cases to:

```bash
      --dry-run)             WANT_DRY_RUN=1; shift ;;
      -d|--debug)            WANT_DEBUG=1; shift ;;
```

At the end of `parse_args` (after the image check), add:

```bash
  if [[ "$WANT_DEBUG" -eq 1 && "$WANT_DRY_RUN" -eq 1 ]]; then
    echo "Error: --debug and --dry-run are mutually exclusive." >&2
    exit 2
  fi
  if [[ "$WANT_DEBUG" -eq 1 ]]; then MODE="debug"; fi
  if [[ "$WANT_DRY_RUN" -eq 1 ]]; then MODE="dry-run"; fi
```

(Remove the earlier `MODE="dry-run"` assignment from Task 4's `--dry-run` case — it's now resolved at the end. The `MODE="normal"` default stays.)

Add to `usage()` Options:

```
  -d, --debug             Start the runner container idle (sleep infinity) for
                          manual `docker exec` debugging; do NOT run ansible
```

Add a debug example to `usage()` after the existing Example:

```
  ./run-ansible.sh -d --playbook ping.yml --inventory taipei/multinode.ini --limit node1
```

Add `run_debug()` after `run_dry_run()`:

```bash
# ── Debug: start an idle container for manual `docker exec` poking ───────────
# No --rm (container is kept), trap disarmed (clone dir is kept) — both are
# needed so the operator can exec in and inspect /inventory and networking.
run_debug() {
  if [[ -n "$RUN_ID" ]]; then
    DEBUG_CONTAINER="ansible-debug-$RUN_ID"
  else
    DEBUG_CONTAINER="ansible-debug-$(basename "$CLONE_DIR" | sed 's/^ansible-inventory\.//')"
  fi

  print_summary

  if [[ "$PULL" -eq 1 ]]; then
    echo ">> Pulling latest image: $IMAGE"
    docker pull "$IMAGE"
  fi

  # Keep the clone dir alive for the running container.
  trap - EXIT

  docker run -d --name "$DEBUG_CONTAINER" \
    --add-host host.docker.internal:host-gateway \
    -v "$CLONE_DIR":/inventory:ro \
    -v "$SSH_KEY":/root/.ssh/id_key:ro \
    -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
    -e ANSIBLE_COLLECTIONS_PATH=/collections \
    "$IMAGE" \
    sleep infinity

  local manual="ansible-playbook -i /inventory/$INVENTORY /playbooks/$PLAYBOOK"
  [[ -n "$TAGS"  ]] && manual="$manual --tags $TAGS"
  [[ -n "$LIMIT" ]] && manual="$manual --limit $LIMIT"

  cat <<EOF
══════════════ DEBUG MODE ══════════════
Container '$DEBUG_CONTAINER' is running (sleep infinity).

Enter it:
  docker exec -it $DEBUG_CONTAINER bash

Run the playbook manually inside:
  $manual

When done, clean up:
  docker rm -f $DEBUG_CONTAINER
  rm -rf $CLONE_DIR
══════════════════════════════════════════
EOF
  exit 0
}
```

Update `main()`'s dispatch:

```bash
  case "$MODE" in
    debug)   run_debug ;;
    dry-run) run_dry_run ;;
    *)       run_normal ;;
  esac
```

- [ ] **Step 4: Run the new tests and the full suite**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd deploy-service
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(ansible): add -d/--debug idle-container mode

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Help/docs polish + final verification

Confirm `usage()` reflects every new flag, run the entire repo test suite, and verify the shell script parses cleanly.

**Files:**
- Modify (if gaps found): `deploy-service/ansible/run-ansible.sh`
- Possibly update: `deploy-service/ansible/README.md` (if it documents flags)

- [ ] **Step 1: Syntax-check the script**

Run: `bash -n deploy-service/ansible/run-ansible.sh && echo OK`
Expected: `OK` (no parse errors).

- [ ] **Step 2: Confirm help shows all new flags**

Run: `bash deploy-service/ansible/run-ansible.sh --help`
Expected: output lists `--image-tag`, `--dry-run`, `-d, --debug`, and the debug example.

- [ ] **Step 3: Check README for flag documentation**

Run: `grep -n "\-\-image\|\-\-dry-run\|\-\-debug\|run-ansible" deploy-service/ansible/README.md`
If the README enumerates flags, add the three new ones in the same style. If it does not document individual flags, no change needed.

- [ ] **Step 4: Run the full repo test suite**

Run: `cd deploy-service && APP_ENV=test uv run pytest tests/ -v`
Expected: all PASS (no regressions anywhere, not just the ansible test).

- [ ] **Step 5: Commit any doc/help fixes**

```bash
cd deploy-service
git add ansible/run-ansible.sh ansible/README.md
git commit -m "docs(ansible): document --image-tag, --dry-run, --debug flags

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(If Steps 1–4 found nothing to change, skip this commit.)

---

## Self-Review notes

- **Spec coverage:** logging strengthening + summary + full docker-run (Task 2); `--dry-run` coexisting with `DRYRUN=1` (Task 4); `-d/--debug` idle container + guidance + keep clone + no sidecar (Task 5); `--image-tag` + mutual exclusion (Task 3); both mutual-exclusion rules (Tasks 3 & 5); refactor into functions (Task 1); help/docs (Task 6). All spec sections map to a task.
- **Preserved behavior:** Task 1 is gated by the unchanged existing suite; every later task re-runs the full file.
- **Type/name consistency:** `MODE` values (`normal`/`debug`/`dry-run`), function names (`parse_args`, `resolve_log_file`, `clone_inventory`, `build_cmd_args`, `print_summary`, `print_docker_run`, `run_normal`, `run_dry_run`, `run_debug`, `main`), and container name `ansible-debug-<id>` are used consistently across tasks. Task 5 supersedes Task 4's inline `MODE="dry-run"` assignment (called out explicitly).
- **Mutual-exclusion timing:** all exclusion checks live in `parse_args`, before the `DRYRUN=1` early-exit in `resolve_log_file`, so `_run` (which sets `DRYRUN=1`) still exercises them.
