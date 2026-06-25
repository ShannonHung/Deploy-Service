# CommandService Wave 2 — Collaborator Split Design

**Date:** 2026-06-26
**Branch:** `feat/command-service-clean-code` (continues from Wave 1, branched from `develop`)
**Target file:** `app/services/command_service.py` (~1138 lines post-Wave-1)
**Prerequisite:** Wave 1 landed green (commits `093be92`..`172511b`). This is the
"Wave 2 — Full split" deferred by `2026-06-25-command-service-clean-code-design.md`,
now confirmed by the user.

## Motivation

After Wave 1, `command_service.py` is cleaner (helpers extracted, `PipelineBuilder`
split out, pool access wrapped) but `CommandService` is still a ~1138-line God Class
owning: request validation, host resolution, pipeline assembly, SSH connection, three
execution modes, output policy, kill lifecycle, cross-pod recovery, log tailing, and
graceful shutdown. Changing one concern still means reading past several unrelated ones.

Wave 1 deliberately made the boundaries cleaner *first*, so Wave 2 is a **move, not a
rewrite**: method bodies are relocated verbatim into focused collaborators, with only
the call wiring (`self._x` → `self._collaborator._x`, and pool access → `command_pool`)
rewritten. `CommandService` becomes a thin facade composing the collaborators.

## Non-Goals

- **No behavioural change.** Every task is a pure move/refactor; existing tests stay green.
- **No DI restructuring.** `get_command_service` (`app/core/dependencies.py:187`) stays a
  per-request factory. Routers keep depending on `CommandService`.
- No change to the anti-injection architecture (`shlex`-positional args,
  `_validate_anti_injection`, regex/blacklist layers).
- No new public API surface; the facade's public method names are preserved.
- No unrelated refactoring outside the listed tasks.

## Hard Constraints (load-bearing facts)

1. **Process-scoped shared state stays module-level.** `_local_running_commands` + the
   execution semaphore must outlive any request-scoped service (per Wave 1 decision).
   Wave 2 *moves* them to a neutral module (`command_pool.py`) but keeps them module-level
   — never onto `self` of any collaborator.
2. **Anti-injection is untouched.** Moved verbatim with `CommandExecutor`.
3. **Public API + DI unchanged.** `CommandService` keeps `execute_command`,
   `kill_command`, `get_command_trace`, `get_command_execution_result`,
   `list_running_commands`, `shutdown_gracefully`, `get_user_commands`,
   `get_command_info`. All delegate.
4. **Backward-compatible module surface.** Existing tests import `_decode`,
   `CommandService`, `CommandExecutionException` from `command_service`, and call
   `cs.pool_add` / `cs.pool_remove`. The `command_service` module **re-exports** the
   moved names (`pool_*`, `_get_semaphore`, `_decode`) so those imports/usages keep
   resolving — minimizing test churn.

## Workflow Rules

- All work on `feat/command-service-clean-code`.
- Run git from `deploy-service/` (the repo root is `deploy-service/.git`).
- **Each task ends green:** `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'` must pass.
- One focused commit per task.

---

## Target Architecture

### Shared foundation (no concern logic)

- **`app/services/command_pool.py`** *(new)* — holds `_local_running_commands`, the
  semaphore, and `pool_add / pool_get / pool_remove / pool_size / pool_command_ids /
  _get_semaphore`. Neutral home importable by everyone without import cycles.

### Support collaborators (cross-concern, injected into the others)

- **`SshSupport`** (`app/services/command_ssh.py`) — `_connect_to_control_node`,
  `_load_ssh_config`. Used by Executor, Lifecycle, Trace, and the poll endpoint.
- **`StateHelpers`** (`app/services/command_state_helpers.py`) — `_get_state_or_404`,
  `_heal_from_marker`, `_read_run_exit_marker`, `_exit_marker_path`. Used by poll,
  Trace, Lifecycle.

### Concern collaborators

- **`CommandExecutor`** (`app/services/command_executor.py`) — `_prepare_execution`,
  `_load_user_whitelist`, `_validate_anti_injection`, `PipelineBuilder` usage,
  `_compute_log_path`, `_connect`, `_build_step_wrapper`, `_execute_pipeline`,
  `_collect_output`, `_handle_async_execution`, `_handle_fire_and_forget`,
  `_apply_output_policy`, `_store_result`, `_check_capacity`, `execute_command`.
- **`CommandLifecycle`** (`app/services/command_lifecycle.py`) — `kill_command`,
  `_do_kill_via_connection`, `list_running_commands`, `shutdown_gracefully`.
- **`CommandTrace`** (`app/services/command_trace.py`) — `_read_remote_log`,
  `get_command_trace`.

### Facade

`CommandService` (stays in `command_service.py`) constructs the support collaborators,
then the three concern collaborators (injecting support + repos), and delegates its
public methods. `get_command_execution_result` stays on the facade and uses
`StateHelpers` + `SshSupport` directly (it spans state-load + heal + ssh-read and does
not belong to a single concern collaborator).

### Data flow

```
router → CommandService (facade)
           ├─ SshSupport ─────────────┐ (shared)
           ├─ StateHelpers ───────────┤ (shared)
           ├─ CommandExecutor(ssh, state, repo, inventory_repo)   → command_pool.*
           ├─ CommandLifecycle(ssh, state, repo)                  → command_pool.*
           ├─ CommandTrace(ssh, state, repo)
           └─ get_command_execution_result → StateHelpers + SshSupport
```

Pool access is module-level (`command_pool.pool_*`); no collaborator owns it.
Collaborators receive dependencies via constructor; no back-references to the facade.

---

## Task Plan (bottom-up; each independently green)

### Task 1 — Extract `command_pool.py`
Move `_local_running_commands`, semaphore, `pool_*`, `_get_semaphore` into
`app/services/command_pool.py`. In `command_service.py`, import them and **re-export**
(`from app.services.command_pool import pool_add, pool_get, ...`) so `cs.pool_add`,
`cs.pool_remove`, and `cs._get_semaphore` still resolve. Update internal call sites to
use `command_pool.*` (or the re-exported names). Coupled tests
(`test_command_kill_state.py`, `test_command_detached_spawn.py`) keep working via the
re-export — no test edits required.
**Acceptance:** suite green; `grep _local_running_commands command_service.py` shows only
the import line.

### Task 2 — Extract `SshSupport`
Move `_connect_to_control_node`, `_load_ssh_config` into `command_ssh.py` as
`SshSupport`. Facade builds `self._ssh = SshSupport(...)` and its methods delegate;
internal callers (poll, heal path) use `self._ssh`.
**Acceptance:** suite green.

### Task 3 — Extract `StateHelpers`
Move `_get_state_or_404`, `_heal_from_marker`, `_read_run_exit_marker`,
`_exit_marker_path` into `command_state_helpers.py`. `_heal_from_marker` depends on the
repo (for `update_if`) and on reading the marker — inject `repo` + `SshSupport`. Facade
builds `self._state = StateHelpers(...)`; `get_command_execution_result` uses it.
**Acceptance:** suite green; `test_command_orphan_heal.py` passes.

### Task 4 — Extract `CommandTrace`
Move `_read_remote_log`, `get_command_trace` into `command_trace.py` (depends on
`SshSupport` + `StateHelpers`). Facade `get_command_trace` delegates.
**Acceptance:** suite green; `test_command_trace.py` passes.

### Task 5 — Extract `CommandLifecycle`
Move `kill_command`, `_do_kill_via_connection`, `list_running_commands`,
`shutdown_gracefully` into `command_lifecycle.py` (depends on `SshSupport`, `repo`,
`command_pool.*`). Facade delegates.
**Acceptance:** suite green; `test_command_kill_state.py`, `test_command_kill_api.py`,
`test_command_service_running.py` pass.

### Task 6 — Extract `CommandExecutor`
Move the execution cluster (prep, connect-for-run, pipeline build/run, output policy,
result store, capacity check, the three execution handlers, `execute_command`) into
`command_executor.py` (depends on `SshSupport`, `StateHelpers` if needed, `repo`,
`inventory_repo`, `PipelineBuilder`, `command_pool.*`). Facade `execute_command`
delegates. `_check_capacity` reads `command_pool.pool_size()`.
**Acceptance:** suite green; execution + anti-injection + capacity tests pass.

### Task 7 — Slim the facade + final verification
Confirm `CommandService.__init__` only wires collaborators and public methods only
delegate. Grep for stale `self._<moved_method>` references. Run full non-e2e suite.
**Acceptance:** facade is a thin composer; suite green; no stale refs.

---

## Testing Strategy

- The full non-e2e suite (`-m 'not e2e'`) green at every task is the primary safety net
  for a behaviour-preserving move; the existing integration + unit tests already exercise
  execute / kill / poll / trace / heal / capacity / shutdown.
- Add new unit tests only where a collaborator gains a genuinely independent seam (e.g.
  `StateHelpers` heal logic in isolation). Do **not** manufacture tests for pure
  relocations already covered.
- Backward-compatible re-exports keep `test_command_decode.py`,
  `test_command_kill_state.py`, `test_command_detached_spawn.py` passing without edits.

## Error Handling

Unchanged. Exception types, `NotFoundException` detail payloads, and the Wave 1 Task 6
silent-return log lines move verbatim with their methods.

## Risks & Mitigations

- **Import cycles:** mitigated by the neutral `command_pool.py` and by collaborators
  never importing the facade.
- **Pool semantics regression:** pool stays module-level in `command_pool.py`; full suite
  exercises cross-pod kill/poll/backpressure.
- **Hidden cross-method coupling surfacing mid-split:** bottom-up sequencing (foundation
  → support → concerns) means each extraction only depends on already-extracted pieces.
- **Test churn:** re-exports from `command_service` preserve existing import paths and
  `cs.pool_*` usage.
