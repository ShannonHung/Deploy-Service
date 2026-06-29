# run-ansible.sh — Logging, Debug/Dry-run modes, image-tag, and refactor

**Date:** 2026-06-29
**Status:** Approved (design)
**File touched:** `deploy-service/ansible/run-ansible.sh` (full rewrite into functions)
**Tests:** `deploy-service/tests/integration/test_run_ansible_script.py` (extend)

## Goal

`run-ansible.sh` works but is hard to operate and read. This change makes it:

1. **Observable** — clearly log who/what was cloned, where, and the exact commands run.
2. **Debuggable** — a `-d/--debug` mode that starts the runner container idle so the
   operator can `docker exec` in and test the in-container environment (networking, SSH).
3. **Inspectable without side effects** — a `--dry-run` flag that clones inventory and
   prints everything but never pulls or runs docker.
4. **Easy to pin a version** — a `--image-tag` flag to set just the image tag.
5. **Maintainable** — refactored into well-named functions with a readable `main()`,
   tidy comments, and consistent section formatting, while preserving every existing
   behavior the integration test guarantees.

Non-goals: changing the deploy-service ↔ script contract (EXIT marker, `.exit` sidecar,
`--run-id` semantics), changing the anti-injection model, or touching deploy-service code.

## Behavior to preserve (locked by existing tests)

These MUST remain identical after the refactor — `tests/integration/test_run_ansible_script.py`:

- `DRYRUN=1` env hook: early-exit **before** clone/docker, prints `DRYRUN log file: <path>`.
- `--run-id` validation (`^[A-Za-z0-9_-]+$`), rejects `../evil` with exit 2.
- `--run-id` sets log filename to `<id>.log`; absent → `run.log`.
- `--log-retention-days` validation + pruning of `*.log` older than N days; `0` disables.
- After a real run: append `=== EXIT <code> ===` to the log and (when `--run-id` set)
  write atomic `<id>.exit` sidecar; re-exit with the real ansible/docker exit code.
- No `.exit` sidecar without `--run-id`.
- Anti-injection: discrete-args only, no `eval`, no shell-string building.

## New flags

| Flag | Effect |
|------|--------|
| `-d`, `--debug` | Start runner container idle (`sleep infinity`), print exec/run/cleanup hints, do not run ansible. |
| `--dry-run` | Clone (and delete) inventory, print full summary + docker run/ansible commands, do **not** pull or run docker. Exit 0. |
| `--image-tag <tag>` | Use `shannonhung/ansible-runner:<tag>` as the image. |

### Flag interaction rules

- `--debug` and `--dry-run` are **mutually exclusive** → error, exit 2.
- `--image` and `--image-tag` are **mutually exclusive** → error, exit 2.
  (`--image` takes a full name; `--image-tag` applies a tag to the default repo.)

## Logging

Applies to all three modes (normal / debug / dry-run).

1. **Inline line strengthening**
   - Clone line includes the destination: `>> Cloning inventory (ref: <ref>) from <repo> into <clone_dir>`
   - After inventory validation: `>> Inventory resolved: /inventory/<path>`
2. **Centered Run Summary block** printed before execution:
   ```
   ══════════════════ RUN SUMMARY ══════════════════
     Inventory repo : <repo>
     Inventory ref  : <ref>
     Clone dir      : <clone_dir>
     Inventory file : /inventory/<path>
     Playbook       : /playbooks/<playbook>
     Image          : <image>
     SSH key        : <ssh_key>
     Ansible cmd    : <ansible-playbook ...>
     Log file       : <log_file>
   ══════════════════════════════════════════════════
   ```
3. **Full docker run command** printed in normal mode too (mounts/env/add-host), not
   just the ansible portion.

All values come from existing variables; no extra parsing.

## Mode flows

### Normal (unchanged outcome, refactored internals)
parse → validate → clone+validate inventory → print summary → print full docker run →
pull (unless `--no-pull`) → validate ssh key → `docker run --rm ... ansible-playbook ...`
| tee → write EXIT marker + sidecar → re-exit real code.

### `--dry-run`
parse → validate → clone+validate inventory → print summary + full docker run/ansible
commands → **no pull, no docker run** → exit 0. `trap cleanup` still deletes the clone dir.
No EXIT marker / sidecar (nothing ran). Distinct from `DRYRUN=1` (which exits before clone).

### `-d/--debug`
parse → validate → clone+validate inventory → print summary → pull (unless `--no-pull`) →
`docker run -d --name ansible-debug-<run_id-or-random> <same mounts/env/add-host> "$IMAGE" sleep infinity`
(no `--rm`) → **disable `trap cleanup`** (clone dir kept) → print debug guidance → exit 0.
No EXIT marker / sidecar.

Debug guidance (copy-paste, with this run's real paths/args substituted):
```
══════════════ DEBUG MODE ══════════════
Container 'ansible-debug-<id>' is running (sleep infinity).

Enter it:
  docker exec -it ansible-debug-<id> bash

Run the playbook manually inside:
  ansible-playbook -i /inventory/<path> /playbooks/<playbook> [--tags ..] [--limit ..]

When done, clean up:
  docker rm -f ansible-debug-<id>
  rm -rf <clone_dir>
══════════════════════════════════════════
```
(No ping/ssh network probe lines — only enter, manual playbook, cleanup.)

Container name: `ansible-debug-<run_id>` when `--run-id` given, else `ansible-debug-<random>`.

## Refactor shape

Split the one-pass script into focused functions, orchestrated by `main()`:

- `usage()` — help (add new flags + examples).
- `parse_args "$@"` — arg loop; sets globals; enforces mutual-exclusion rules.
- `resolve_image()` — combine `--image` / `--image-tag` / default into `$IMAGE`.
- `resolve_log_file()` — RUN_ID validation + log path (keeps `DRYRUN=1` early-exit).
- `prune_old_logs()` — retention cleanup.
- `clone_inventory()` — mktemp clone dir, `git clone`, traversal + existence validation.
- `build_cmd_args()` — assemble `CMD_ARGS` (discrete args).
- `print_summary()` — the Run Summary block.
- `print_docker_run()` — render the full docker run command (shared by normal/dry-run).
- `run_normal()` — docker run + tee + EXIT marker + sidecar + re-exit.
- `run_debug()` — idle container + guidance.
- `run_dry_run()` — print-only.
- `cleanup()` / `trap` — unchanged for normal/dry-run; not armed for debug.

Comments: keep the load-bearing design notes (DooD path rationale, anti-injection,
EXIT-marker source-of-truth) but trim verbosity; standardize the `── section ──` headers.

## Testing

Extend `tests/integration/test_run_ansible_script.py` (reuse its fake git+docker harness):

- `--dry-run`: with fake docker that would fail if invoked, assert exit 0, summary
  printed, no `docker run` happened (fake docker not executed / no marker written).
- `--debug`: assert it calls `docker run -d ... sleep infinity` (fake docker records argv),
  prints the exec hint and cleanup commands, exits 0, writes no `.exit` sidecar, and
  the clone dir is NOT removed.
- `--image-tag v1.2`: assert summary shows `shannonhung/ansible-runner:v1.2`.
- Mutual exclusion: `--debug --dry-run` → exit 2; `--image --image-tag` → exit 2.
- Re-run the full existing suite unchanged to confirm no regression.

## Risks

- Largest risk is the refactor changing a preserved behavior. Mitigation: the existing
  integration test is the contract; run it green before and after, add the new cases.
- `set -euo pipefail` interactions inside functions (esp. `PIPESTATUS` capture in
  `run_normal`) must be preserved exactly — keep the `set +e` / `set -e` window.
