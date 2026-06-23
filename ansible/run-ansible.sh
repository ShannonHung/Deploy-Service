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
INVENTORY_REPO="https://gitlab.com/ShannonHung/my-ansible-inventory.git"
IMAGE="shannonhung/ansible-runner:latest"

# ── Defaults ──────────────────────────────────────────────────────────────────
PLAYBOOK=""
INVENTORY=""            # path RELATIVE to the inventory repo root, e.g. taipei/multinode.ini
INVENTORY_REF="main"    # branch/tag of the inventory repo to clone
TAGS=""
LIMIT=""
EXTRA_VARS=""
PULL=1                  # docker pull before run; --no-pull disables (for local-built images)
LOG_DIR="$(pwd)/logs"
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
  --log-dir <path>        Host dir to mount for logs (default: ./logs)
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
    --log-dir)        LOG_DIR="$2"; shift 2 ;;
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

# ── Fresh inventory clone (deleted on exit, always latest) ─────────────────────
CLONE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ansible-inventory.XXXXXX")"
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

mkdir -p "$LOG_DIR"

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
echo ">> Logs:    $LOG_DIR/run.log (tee'd from stdout)"

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
  -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
  -e ANSIBLE_COLLECTIONS_PATH=/collections \
  "$IMAGE" \
  "${CMD_ARGS[@]}" 2>&1 | tee "$LOG_DIR/run.log"
