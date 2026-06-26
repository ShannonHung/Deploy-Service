# CommandService Clean-Code Refactor — Design

**Date:** 2026-06-25
**Branch:** `feat/command-service-clean-code` (from `develop`)
**Target file:** `app/services/command_service.py`

## Motivation

`command_service.py` is well-documented and keeps to the layered/dependency-inverted
architecture, but at ~1159 lines `CommandService` has become a God Class: a single
type owns request validation, host resolution, pipeline assembly, SSH connection,
three execution modes, output policy, kill lifecycle, cross-pod recovery, log tailing,
and graceful shutdown. The readability is high; the *local modifiability* is not —
changing one concern means reading past several unrelated ones.

This refactor improves maintainability in two waves. **Wave 1 (gentle)** is a set of
low-risk, independently-verifiable convergences. **Wave 2 (full)** splits the class
into collaborators by responsibility; it is **optional** and gated on Wave 1 landing
green and explicit user confirmation.

## Non-Goals

- No behavioural change. Every task is a refactor; existing tests must stay green.
- No DI restructuring. `get_command_service` remains a per-request factory.
- No unrelated refactoring outside the listed tasks.

## Hard Constraints (load-bearing facts discovered during design)

1. **Process-scoped shared state is intentional.** `get_command_service`
   (`app/core/dependencies.py:187`) is a *per-request* DI factory — every request
   constructs a fresh `CommandService`. `_local_running_commands` and the execution
   semaphore are therefore module-level **on purpose**: the running pool and the
   concurrency gate are process-scoped and must outlive any single request-scoped
   service instance. Moving them onto `self` would give each request an empty pool
   and break cross-pod kill / poll / backpressure. **Decision:** keep them
   module-level; only wrap access behind named helpers (Task 4).

2. **The anti-injection architecture is load-bearing and untouched.** The
   `shlex`-positional-argument design and the `_validate_anti_injection` /
   regex defence layers are not modified by any task here.

3. **Correct types.** Command-argument definitions are `CommandArgumentConfig`
   (`app/domain/command.py:82`); `cmd_config.arguments: List[CommandArgumentConfig]`.

## Workflow Rules

- All work on `feat/command-service-clean-code`, branched from `develop`.
- **Each task ends green:** run `APP_ENV=test uv run pytest tests/ -v` (and the
  CI-aligned `-m 'not e2e'` subset) — a task is not complete until the suite passes.
- One focused commit per task.

---

## Wave 1 — Gentle (this iteration)

### Task 1 — Extract `_decode` helper
**Problem:** The nested-ternary stdout/stderr decode appears twice, nearly verbatim,
at lines 658–659 (`_handle_fire_and_forget`) and 854–855 (`_collect_output`):

```python
out_str = result.stdout if isinstance(result.stdout, str) else result.stdout.decode('utf-8') if result.stdout else ""
```

**Change:** Add a small `_decode(stream) -> str` helper (or module function) that
normalises `bytes | str | None` to `str`. Both call sites delegate to it.
**Acceptance:** Behaviour identical; both call sites use the helper; suite green.

### Task 2 — Extract `_get_state_or_404`
**Problem:** `get_command_execution_result` (127–133) and `get_command_trace`
(317–323) repeat the same `repo.get` / `except CommandExecutionException → raise
NotFoundException` block.
**Change:** Extract an async `_get_state_or_404(command_id) -> CommandState` helper;
both methods call it.
**Acceptance:** Identical `NotFoundException` behaviour and detail payload; suite green.

### Task 3 — Delete dead code + tighten types
**Changes:**
- Remove the no-op `except Exception as e: raise e` in `_execution_task` (949–951).
- Replace bare `arg_defs: list` with `List[CommandArgumentConfig]` in
  `_resolve_command_part` and `_strip_omitted_optionals` (aligning with other
  signatures).
**Acceptance:** No behavioural change; suite green.

### Task 4 — Wrap module-global pool access behind named helpers
**Problem:** Tests and service code poke the bare module global directly
(`cs._local_running_commands[...]`, `monkeypatch.setattr(cs_mod, "_local_running_commands", ...)`).
**Change:** Per the design decision, **keep the global where it is**, but introduce
named accessor helpers — e.g. `pool_add(command_id, entry)`, `pool_remove(command_id)`,
`pool_get(command_id)`, `pool_size()`, plus the existing `_get_semaphore()`. All
service-internal direct manipulations of `_local_running_commands` route through these.
**Test impact (allowed):** Update the coupled test files to call the helpers instead
of poking the module variable:
- `tests/unit/test_command_kill_state.py`
- `tests/unit/test_command_service_errors.py`
- `tests/unit/test_command_detached_spawn.py`
- (and any other file referencing `_local_running_commands` surfaced by grep)
**Acceptance:** No behavioural change; cross-request sharing semantics preserved;
tests use helpers; suite green.

### Task 5 — Extract `PipelineBuilder` (pure, I/O-free)
**Problem:** `_resolve_command_part`, `_strip_omitted_optionals`, and `_build_pipeline`
are pure functions buried in the God Class.
**Change:** Move them into a dedicated, independently unit-testable `PipelineBuilder`
class (or module). `CommandService` composes it (`self._pipeline_builder.build(context)`).
No side effects, no I/O — trivially testable in isolation.
**Acceptance:** `_build_pipeline` output byte-identical for existing cases; existing
pipeline tests pass (adjusted only for the new call path); suite green.

### Task 6 — Converge silent exception swallowing in `kill_command`
**Problem:** Several `except CommandExecutionException: return` blocks (and the
cross-pod kill failure at ~1120) fail silently, making failures hard to trace; the
KILLING-state recovery relies on the heal path picking it up later.
**Change:** Add `logger` lines (with `command_id` in `extra`) at each silent return /
swallow so the path is observable. **Control flow unchanged** — observability only.
**Acceptance:** No behavioural change; new log lines present; suite green.

---

## Wave 2 — Full split (OPTIONAL — confirm before entering)

> **Gate:** Do NOT start Wave 2 until all of Wave 1 has landed green AND the user
> explicitly confirms. Listed here for visibility only.

Split `CommandService` into collaborators by responsibility, leaving `CommandService`
as a thin facade that composes them:

- **`CommandExecutor`** — `_connect`, `_execute_pipeline`, `_collect_output`,
  `_handle_async_execution`, `_handle_fire_and_forget`, step-wrapper building.
- **`CommandLifecycle`** — `kill_command`, `_do_kill_via_connection`,
  `_heal_from_marker`, `_read_run_exit_marker`, `list_running_commands`,
  `shutdown_gracefully`.
- **`CommandTrace`** — `_read_remote_log`, `get_command_trace`, the log-viewer path.

Boundaries are far cleaner *after* Wave 1 (helpers extracted, pipeline builder
independent, pool access wrapped), so Wave 2 becomes a *move*, not a *rewrite*. Wave 2
will get its own brainstorming pass to nail collaborator interfaces and ownership of
the module-global pool before any code moves.

## Risks & Mitigations

- **Pool semantics regression (Task 4):** keeping the global module-level (not `self`)
  preserves cross-request sharing; helpers are a thin façade. Mitigated by the full
  suite, which already exercises cross-pod kill/poll paths.
- **Pipeline output drift (Task 5):** mitigated by existing pipeline unit tests
  asserting `List[List[str]]` output; Task 5 only relocates pure functions.
