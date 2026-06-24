#!/usr/bin/env bash
#
# run-ansible.sh — launch the ansible runner image to execute a playbook.
#
# This is the script deploy-service will eventually invoke (via the SSH command
# whitelist). For now it's standalone, to prove the path:
#   clone inventory -> docker run ansible image -> ansible-playbook -> SSH -> nodes
#
# "Always latest" guarantees:
#   * The inventory repo is cloned FRESH on every run and deleted afterward
#     (trap), so stale inventory can never be reused.
#   * The runner image is `docker pull`-ed before running, because the team
#     re-tags `latest` every week after code review (same name, new content).
#
# All user-supplied values are passed as discrete arguments — never interpolated
# into a shell string — to keep the anti-injection guarantees deploy-service
# relies on. Do NOT add `eval` or string-built commands here.

set -euo pipefail

# ── Fixed config (not user-overridable by design) ─────────────────────────────
# Fixed inventory repo. INVENTORY_REPO env var can override it for local testing
# (e.g. a file:// path before it's pushed to GitLab); leave unset in production.
INVENTORY_REPO="${INVENTORY_REPO:-https://gitlab.com/ShannonHung/my-ansible-inventory.git}"
IMAGE="shannonhung/ansible-runner:latest"

# ── Defaults ──────────────────────────────────────────────────────────────────
PLAYBOOK=""
INVENTORY=""            # path RELATIVE to the inventory repo root, e.g. taipei/multinode.ini
INVENTORY_REF="main"    # branch/tag of the inventory repo to clone
TAGS=""
LIMIT=""
EXTRA_VARS=""
PULL=1                  # docker pull before run; --no-pull disables (for local-built images)
PLAYBOOK_DIR=""         # host dir bind-mounted over the image's /playbooks; empty → use image's baked playbooks
LOG_DIR="$(pwd)/logs"
RUN_ID=""               # per-run id from deploy-service; when set, log file is <run_id>.log
LOG_RETENTION_DAYS=3    # self-cleaning: prune <log-dir>/*.log older than this many days
SSH_KEY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/ssh_keys/client_key"

usage() {
  cat <<'EOF'
Usage: run-ansible.sh --playbook <file> --inventory <repo-relative-path> [options]

Required:
  --playbook <file>       Playbook filename under playbooks/ (e.g. ping.yml)
  --inventory <path>      Inventory path RELATIVE to the inventory repo root
                          (e.g. taipei/multinode.ini, taipei/bm_multinode.ini)

Options:
  --inventory-ref <ref>   Branch/tag of the inventory repo to clone (default: main)
  --tags <tags>           Comma-separated ansible --tags
  --limit <pattern>       ansible --limit host/group pattern (e.g. node1)
  --extra-vars <k=v ...>  ansible --extra-vars string
  --image <name>          Runner image (default: shannonhung/ansible-runner:latest)
  --no-pull               Skip `docker pull` (use a locally-built image)
  --playbook-dir <path>   Host dir bind-mounted over the image's /playbooks, so
                          local playbooks (not baked into the image) are usable.
                          Unset → use the image's built-in /playbooks.
  --log-dir <path>        Host dir to mount for logs (default: ./logs)
  --run-id <id>           Per-run id; log is written to <log-dir>/<id>.log
                          (must match ^[A-Za-z0-9_-]+$). Unset → run.log.
  --log-retention-days <n>  Delete <log-dir>/*.log older than n days at start
                            of each run (default: 3; 0 disables cleanup)
  --ssh-key <path>        SSH private key to mount (default: ../data/ssh_keys/client_key)
  -h, --help              Show this help

The inventory repo (fixed) is cloned fresh each run and removed afterward:
  https://gitlab.com/ShannonHung/my-ansible-inventory.git

Example:
  ./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini --limit node1
EOF
}

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --playbook)       PLAYBOOK="$2"; shift 2 ;;
    --inventory)      INVENTORY="$2"; shift 2 ;;
    --inventory-ref)  INVENTORY_REF="$2"; shift 2 ;;
    --tags)           TAGS="$2"; shift 2 ;;
    --limit)          LIMIT="$2"; shift 2 ;;
    --extra-vars)     EXTRA_VARS="$2"; shift 2 ;;
    --image)          IMAGE="$2"; shift 2 ;;
    --no-pull)        PULL=0; shift ;;
    --playbook-dir)   PLAYBOOK_DIR="$2"; shift 2 ;;
    --log-dir)        LOG_DIR="$2"; shift 2 ;;
    --run-id)         RUN_ID="$2"; shift 2 ;;
    --log-retention-days) LOG_RETENTION_DAYS="$2"; shift 2 ;;
    --ssh-key)        SSH_KEY="$2"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$PLAYBOOK" || -z "$INVENTORY" ]]; then
  echo "Error: --playbook and --inventory are required." >&2
  usage
  exit 2
fi
if [[ ! -f "$SSH_KEY" ]]; then
  echo "Error: ssh key not found: $SSH_KEY" >&2
  exit 2
fi
# If a host playbook dir is given, it must exist (it's bind-mounted into the
# container over /playbooks). Empty → fall back to the image's baked playbooks.
if [[ -n "$PLAYBOOK_DIR" && ! -d "$PLAYBOOK_DIR" ]]; then
  echo "Error: --playbook-dir not found: $PLAYBOOK_DIR" >&2
  exit 2
fi

# ── Per-run log file + self-cleaning ──────────────────────────────────────────
# RUN_ID is supplied by deploy-service (a UUID) and becomes a filename, so we
# validate it strictly. An empty RUN_ID keeps the legacy single-file behaviour
# (run.log) for standalone use.
if [[ -n "$RUN_ID" ]]; then
  if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "Error: --run-id must match ^[A-Za-z0-9_-]+$" >&2
    exit 2
  fi
  LOG_FILE="$LOG_DIR/$RUN_ID.log"
else
  LOG_FILE="$LOG_DIR/run.log"
fi

# Validate retention is a non-negative integer.
if [[ ! "$LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  echo "Error: --log-retention-days must be a non-negative integer." >&2
  exit 2
fi

mkdir -p "$LOG_DIR"

# Self-cleaning: prune old run logs BEFORE starting work, so a long-running or
# killed run never skips cleanup. Guarded so an empty/undefined LOG_DIR can
# never widen the delete scope. Concurrent runs are safe: only files older than
# the window are removed, never the in-flight <run_id>.log. 0 disables cleanup.
if [[ "$LOG_RETENTION_DAYS" -gt 0 && -n "$LOG_DIR" && -d "$LOG_DIR" ]]; then
  find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime "+$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
fi

# Build the optional playbook bind-mount (host dir over the image's /playbooks).
PLAYBOOK_MOUNT_ARGS=()
if [[ -n "$PLAYBOOK_DIR" ]]; then
  PLAYBOOK_MOUNT_ARGS=(-v "$PLAYBOOK_DIR":/playbooks:ro)
fi

# Test/inspection hook: print the resolved log path and exit before any docker
# or git work. Used by the script's unit test (no network/docker required).
if [[ "${DRYRUN:-0}" == "1" ]]; then
  echo "DRYRUN log file: $LOG_FILE"
  if [[ -n "$PLAYBOOK_DIR" ]]; then
    echo "DRYRUN playbook mount: $PLAYBOOK_DIR -> /playbooks"
  fi
  exit 0
fi

# ── Fresh inventory clone (deleted on exit, always latest) ─────────────────────
# DooD note: the clone dir is later bind-mounted into the ansible container via
# `docker run -v "$CLONE_DIR":/inventory`. When this script runs inside a
# control_node (Docker-out-of-Docker), that -v resolves on the HOST daemon, so
# $CLONE_DIR must be a path the HOST can see. We therefore clone into a dir
# beside this script (which is mounted host-consistently), NOT control_node's
# private /tmp. Override with CLONE_PARENT if needed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLONE_PARENT="${CLONE_PARENT:-$SCRIPT_DIR/.run-tmp}"
mkdir -p "$CLONE_PARENT"
CLONE_DIR="$(mktemp -d "$CLONE_PARENT/ansible-inventory.XXXXXX")"
cleanup() {
  rm -rf "$CLONE_DIR"
}
trap cleanup EXIT

echo ">> Cloning inventory ($INVENTORY_REF) from $INVENTORY_REPO"
git clone --depth 1 --branch "$INVENTORY_REF" "$INVENTORY_REPO" "$CLONE_DIR"

# Validate the requested inventory file exists inside the fresh clone. Reject
# path traversal so the relative path can't escape the cloned repo.
case "$INVENTORY" in
  /*|*..*) echo "Error: --inventory must be a relative path inside the repo." >&2; exit 2 ;;
esac
if [[ ! -f "$CLONE_DIR/$INVENTORY" ]]; then
  echo "Error: inventory file not found in repo: $INVENTORY" >&2
  echo "Available inventory files:" >&2
  find "$CLONE_DIR" -name '*.ini' -o -name '*.yml' -path '*inventor*' 2>/dev/null | sed "s#$CLONE_DIR/#  #" >&2 || true
  exit 2
fi

# ── Always-latest image ───────────────────────────────────────────────────────
if [[ "$PULL" -eq 1 ]]; then
  echo ">> Pulling latest image: $IMAGE"
  docker pull "$IMAGE"
fi

# ── Build the full command (discrete args, no eval) ───────────────────────────
# The image has NO ENTRYPOINT (it's a plain ubuntu+ansible image), so we pass
# the executable explicitly. Everything is baked at root in the company image:
#   /ansible.cfg  /playbooks  /collections
# and the fresh inventory clone is mounted over /inventory.
CMD_ARGS=(ansible-playbook -i "/inventory/$INVENTORY" "/playbooks/$PLAYBOOK")
[[ -n "$TAGS"       ]] && CMD_ARGS+=(--tags "$TAGS")
[[ -n "$LIMIT"      ]] && CMD_ARGS+=(--limit "$LIMIT")
[[ -n "$EXTRA_VARS" ]] && CMD_ARGS+=(--extra-vars "$EXTRA_VARS")

echo ">> Running: ${CMD_ARGS[*]}"
echo ">> Logs:    $LOG_FILE (tee'd from stdout)"

# --add-host host.docker.internal:host-gateway lets the container reach the
# host-published SSH ports (node1=2222, node2=2223) on Linux too (it's implicit
# on Docker Desktop / macOS).
#
# The company image doesn't write an ansible log file, so we capture the
# container's stdout/stderr and tee it to the host. `set -o pipefail` (above)
# ensures a non-zero ansible exit still propagates through the tee.
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  -v "$CLONE_DIR":/inventory:ro \
  -v "$SSH_KEY":/root/.ssh/id_key:ro \
  "${PLAYBOOK_MOUNT_ARGS[@]}" \
  -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
  -e ANSIBLE_COLLECTIONS_PATH=/collections \
  "$IMAGE" \
  "${CMD_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
