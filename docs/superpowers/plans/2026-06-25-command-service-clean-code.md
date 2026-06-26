# CommandService Clean-Code Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the maintainability of `app/services/command_service.py` through six low-risk, behaviour-preserving convergences, without changing runtime behaviour.

**Architecture:** Wave 1 only — extract duplicated helpers, delete dead code, wrap module-global pool access behind named helpers (keeping the global module-level by design), and relocate the pure pipeline-building functions into their own class. Wave 2 (collaborator split) is deferred and out of scope for this plan.

**Tech Stack:** Python 3, FastAPI, asyncssh, pytest (`asyncio_mode=auto`), uv.

## Global Constraints

- All work on branch `feat/command-service-clean-code` (already created from `develop`).
- Run git from `deploy-service/` — the repo root is `deploy-service/.git`, NOT the `antigravity-fastapi/` top-level dir.
- **No behavioural change.** Every task is a pure refactor.
- **No DI restructuring.** `get_command_service` stays a per-request factory.
- `_local_running_commands` and the execution semaphore **stay module-level** (process-scoped shared state is intentional — per-request services must share one pool). Only access is wrapped.
- The `shlex`-positional-argument anti-injection architecture and `_validate_anti_injection` are **not modified**.
- Command-argument type is `CommandArgumentConfig` (`app/domain/command.py:82`).
- **Each task ends green:** `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'` must pass before the task is complete. One focused commit per task.
- Test command for a single file: `APP_ENV=test uv run pytest tests/unit/<file>.py -v`.

## File Structure

- **Modify** `app/services/command_service.py` — all six tasks touch this file.
- **Create** `app/services/pipeline_builder.py` (Task 5) — pure, I/O-free pipeline assembly.
- **Modify** tests coupled to the module global (Task 4):
  `tests/unit/test_command_kill_state.py`, `tests/unit/test_command_service_errors.py`, `tests/unit/test_command_detached_spawn.py`.
- **Create** `tests/unit/test_pipeline_builder.py` (Task 5).
- **Create** `tests/unit/test_command_decode.py` (Task 1).

---

### Task 1: Extract `_decode` helper for SSH stream normalisation

**Files:**
- Modify: `app/services/command_service.py` (lines 658-659 in `_handle_fire_and_forget`, 854-855 in `_collect_output`)
- Test: `tests/unit/test_command_decode.py` (create)

**Interfaces:**
- Produces: module-level function `_decode(stream: Any) -> str` — normalises `bytes` (utf-8 decoded), `str` (passed through), and falsy/`None` (→ `""`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_decode.py`:

```python
from app.services.command_service import _decode


def test_decode_bytes():
    assert _decode(b"hello") == "hello"


def test_decode_str_passthrough():
    assert _decode("hello") == "hello"


def test_decode_none_is_empty():
    assert _decode(None) == ""


def test_decode_empty_bytes_is_empty():
    assert _decode(b"") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_decode.py -v`
Expected: FAIL with `ImportError: cannot import name '_decode'`.

- [ ] **Step 3: Add the helper**

In `app/services/command_service.py`, add near the other module-level helpers (just after `_get_semaphore`, around line 49):

```python
def _decode(stream: Any) -> str:
    """Normalise an asyncssh stdout/stderr stream (bytes | str | None) to str."""
    if not stream:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8")
    return str(stream)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_decode.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Replace both call sites**

In `_collect_output` (currently lines 853-857), replace:

```python
        stdout_data, stderr_data = await final_process.communicate()
        out_str = stdout_data.decode('utf-8') if isinstance(stdout_data, bytes) else str(stdout_data) if stdout_data else ""
        err_str = stderr_data.decode('utf-8') if isinstance(stderr_data, bytes) else str(stderr_data) if stderr_data else ""

        final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
```

with:

```python
        stdout_data, stderr_data = await final_process.communicate()
        out_str = _decode(stdout_data)
        err_str = _decode(stderr_data)

        final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
```

In `_handle_fire_and_forget` (currently lines 658-660), replace:

```python
            out_str = result.stdout if isinstance(result.stdout, str) else result.stdout.decode('utf-8') if result.stdout else ""
            err_str = result.stderr if isinstance(result.stderr, str) else result.stderr.decode('utf-8') if result.stderr else ""
            final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
```

with:

```python
            out_str = _decode(result.stdout)
            err_str = _decode(result.stderr)
            final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
```

- [ ] **Step 6: Run the full suite to verify no regression**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline + 4 new).

- [ ] **Step 7: Commit**

```bash
git add app/services/command_service.py tests/unit/test_command_decode.py
git commit -m "refactor(command): extract _decode helper for SSH stream normalisation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Extract `_get_state_or_404` helper

**Files:**
- Modify: `app/services/command_service.py` (`get_command_execution_result` 127-133, `get_command_trace` 317-323)
- Test: existing `tests/unit/test_command_orphan_heal.py` + `tests/unit/test_command_trace.py` exercise the NotFound path; verify they still pass.

**Interfaces:**
- Consumes: `self.repo.get`, `CommandExecutionException`, `NotFoundException`.
- Produces: `async def _get_state_or_404(self, command_id: str) -> CommandState`.

- [ ] **Step 1: Add the helper method**

In `app/services/command_service.py`, add a method on `CommandService` (place it just before `get_command_execution_result`, ~line 114):

```python
    async def _get_state_or_404(self, command_id: str) -> CommandState:
        """Load a CommandState from Redis or raise NotFoundException.

        Shared by the poll and trace endpoints — both 404 on an unknown id.
        """
        try:
            return await self.repo.get(command_id)
        except CommandExecutionException as exc:
            raise NotFoundException(
                f"Command {command_id} not found.",
                detail={"command_id": command_id},
            ) from exc
```

- [ ] **Step 2: Use it in `get_command_execution_result`**

Replace the opening block (currently 127-133):

```python
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException as exc:
            raise NotFoundException(
                f"Command {command_id} not found.",
                detail={"command_id": command_id},
            ) from exc
```

with:

```python
        state = await self._get_state_or_404(command_id)
```

- [ ] **Step 3: Use it in `get_command_trace`**

Replace the opening block (currently 317-323):

```python
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException as exc:
            raise NotFoundException(
                f"Command {command_id} not found.",
                detail={"command_id": command_id},
            ) from exc
```

with:

```python
        state = await self._get_state_or_404(command_id)
```

- [ ] **Step 4: Run the affected unit tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_orphan_heal.py tests/unit/test_command_trace.py -v`
Expected: PASS — the NotFoundException detail payload is unchanged.

- [ ] **Step 5: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_service.py
git commit -m "refactor(command): extract _get_state_or_404 to dedupe NotFound lookup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Delete dead code and tighten argument types

**Files:**
- Modify: `app/services/command_service.py` (`_execution_task` 949-951; `_resolve_command_part` 507; `_strip_omitted_optionals` 522)

**Interfaces:**
- Consumes: `CommandArgumentConfig` (already imported? verify — if not, add to the existing `from app.domain.command import (...)` block).
- Produces: no new public surface.

- [ ] **Step 1: Ensure `CommandArgumentConfig` is imported**

Check the import block at the top of `command_service.py` (lines 12-18). If `CommandArgumentConfig` is not listed, add it:

```python
from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    UserCommandWhitelist, CommandWhitelistConfig,
    SSHConnectionConfig, RunningCommandEntry, ExecutionContext,
    CommandState, CommandStatus, HostType,
    CommandLogLine, CommandTraceResponse,
    CommandArgumentConfig,
)
```

- [ ] **Step 2: Remove the no-op except in `_execution_task`**

In `_handle_async_execution`'s inner `_execution_task` (currently 949-951), delete the dead handler:

```python
            except Exception as e:
                # Abort safely inside task wrapper
                raise e
```

The surrounding `try:` body remains; since the handler only re-raised, removing both the `try:` line and the `except` makes the body run unguarded with identical semantics. Replace the whole `try/except` with just its body (de-indented one level). Confirm the body (steps 1-3 of the task: execute pipeline, update pgids, collect output, store result) is now directly inside `async def _execution_task():`.

- [ ] **Step 3: Tighten the two pipeline signatures**

In `_resolve_command_part` (line 507), change `arg_defs: list` → `arg_defs: List[CommandArgumentConfig]`:

```python
    def _resolve_command_part(self, part: str, arguments: Dict[str, Any], arg_defs: List[CommandArgumentConfig], run_id: Optional[str] = None) -> str:
```

In `_strip_omitted_optionals` (line 522), same change:

```python
    def _strip_omitted_optionals(self, command: List[str], arguments: Dict[str, Any], arg_defs: List[CommandArgumentConfig]) -> List[str]:
```

- [ ] **Step 4: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline). The `_execution_task` change is exercised by the async-execution tests; the type change is annotation-only.

- [ ] **Step 5: Commit**

```bash
git add app/services/command_service.py
git commit -m "refactor(command): drop no-op re-raise, type pipeline arg_defs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wrap module-global pool access behind named helpers

**Files:**
- Modify: `app/services/command_service.py` (global at 39; touch-points at 780, 883, 973, 980, 1054, 1083, 1155, 1156)
- Modify: `tests/unit/test_command_kill_state.py`, `tests/unit/test_command_detached_spawn.py`, `tests/unit/test_command_service_errors.py`

**Interfaces:**
- Produces (module-level functions operating on the existing `_local_running_commands` global):
  - `pool_add(command_id: str, entry: RunningCommandEntry) -> None`
  - `pool_get(command_id: str) -> Optional[RunningCommandEntry]`
  - `pool_remove(command_id: str) -> None`
  - `pool_size() -> int`
  - `pool_command_ids() -> List[str]`
- The global `_local_running_commands` stays where it is; helpers are the only sanctioned access path.

- [ ] **Step 1: Add the helper functions**

In `app/services/command_service.py`, just after the `_local_running_commands` declaration (line 39), add:

```python
def pool_add(command_id: str, entry: RunningCommandEntry) -> None:
    """Register a locally-running command in the process-wide pool."""
    _local_running_commands[command_id] = entry


def pool_get(command_id: str) -> Optional[RunningCommandEntry]:
    """Fetch a locally-running command entry, or None if not on this pod."""
    return _local_running_commands.get(command_id)


def pool_remove(command_id: str) -> None:
    """Drop a command from the local pool (no-op if absent)."""
    _local_running_commands.pop(command_id, None)


def pool_size() -> int:
    """Number of commands currently running locally (backpressure gate)."""
    return len(_local_running_commands)


def pool_command_ids() -> List[str]:
    """Snapshot of locally-running command ids (for graceful shutdown)."""
    return list(_local_running_commands.keys())
```

- [ ] **Step 2: Route all in-service touch-points through the helpers**

Apply these exact replacements in `command_service.py`:

- Line 780 `entry = _local_running_commands.get(command_id)` → `entry = pool_get(command_id)`
- Line 883 `_local_running_commands[command_id] = entry` → `pool_add(command_id, entry)`
- Line 973 `_local_running_commands.pop(command_id, None)` → `pool_remove(command_id)`
- Line 980 `if len(_local_running_commands) >= settings.COMMAND_MAX_RUNNING:` → `if pool_size() >= settings.COMMAND_MAX_RUNNING:`
- Line 1054 `entry = _local_running_commands.get(command_id)` → `entry = pool_get(command_id)`
- Line 1083 `entry = _local_running_commands.get(command_id)` → `entry = pool_get(command_id)`
- Line 1155 `logger.info(f"Shutting down {len(_local_running_commands)} running commands gracefully.")` → `logger.info(f"Shutting down {pool_size()} running commands gracefully.")`
- Line 1156 `tasks = [self.kill_command(cmd_id) for cmd_id in list(_local_running_commands.keys())]` → `tasks = [self.kill_command(cmd_id) for cmd_id in pool_command_ids()]`

- [ ] **Step 3: Verify no direct touch-points remain**

Run: `grep -n "_local_running_commands" app/services/command_service.py`
Expected: only the declaration (line ~39) and the five helper bodies — no other call sites.

- [ ] **Step 4: Update coupled tests to use helpers**

In `tests/unit/test_command_kill_state.py`, replace the `import app.services.command_service as cs` usages. Change each:
- `cs._local_running_commands["c1"] = RunningCommandEntry(...)` → `cs.pool_add("c1", RunningCommandEntry(...))`
- `cs._local_running_commands["c1"] = entry` → `cs.pool_add("c1", entry)`
- `cs._local_running_commands.pop("c1", None)` → `cs.pool_remove("c1")`

In `tests/unit/test_command_detached_spawn.py`, same pattern:
- `cs._local_running_commands[cmd_id] = RunningCommandEntry(...)` → `cs.pool_add(cmd_id, RunningCommandEntry(...))`
- `cs._local_running_commands[cmd_id] = entry` → `cs.pool_add(cmd_id, entry)`
- `cs._local_running_commands.pop(cmd_id, None)` → `cs.pool_remove(cmd_id)`

In `tests/unit/test_command_service_errors.py` line 145, the test fills the pool to test capacity. Replace the monkeypatch of the module variable with a helper-based fill. Change:

```python
    monkeypatch.setattr(cs_mod, "_local_running_commands", {"x": object()})
```

to:

```python
    cs_mod.pool_add("x", object())
    monkeypatch.setattr(
        cs_mod.settings, "COMMAND_MAX_RUNNING", 1
    )  # keep existing line if already present
```

…and ensure the entry is cleaned up after the assertion so it doesn't leak into other tests. Wrap the existing body:

```python
    cs_mod.pool_add("x", object())
    try:
        with pytest.raises(ServiceUnavailableException):
            service._check_capacity("test_admin", "rid")
    finally:
        cs_mod.pool_remove("x")
```

(Note: `COMMAND_MAX_RUNNING` is already set to 1 earlier in that test at line 144 — keep that line; only the pool-fill and cleanup change.)

- [ ] **Step 5: Run the three modified test files**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_kill_state.py tests/unit/test_command_detached_spawn.py tests/unit/test_command_service_errors.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite (catch cross-test pool leakage)**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline). If a later test fails on a non-empty pool, a `pool_remove` cleanup was missed — fix the leaking test.

- [ ] **Step 7: Commit**

```bash
git add app/services/command_service.py tests/unit/test_command_kill_state.py tests/unit/test_command_detached_spawn.py tests/unit/test_command_service_errors.py
git commit -m "refactor(command): wrap running-pool access behind named helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Extract `PipelineBuilder` (pure, I/O-free)

**Files:**
- Create: `app/services/pipeline_builder.py`
- Modify: `app/services/command_service.py` (remove `_resolve_command_part` 507-520, `_strip_omitted_optionals` 522-547, `_build_pipeline` 549-567; add a `PipelineBuilder` instance and delegate)
- Create: `tests/unit/test_pipeline_builder.py`

**Interfaces:**
- Consumes: `ExecutionContext` (for `.raw_request.arguments`, `.cmd_config`, `.run_id`), `CommandArgumentConfig`.
- Produces: `class PipelineBuilder` with `build(self, context: ExecutionContext) -> List[List[str]]`. Internal pure methods `resolve_part`, `strip_omitted_optionals` mirror the old private methods.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_pipeline_builder.py`:

```python
from types import SimpleNamespace

from app.services.pipeline_builder import PipelineBuilder


def _arg(name, required=True):
    return SimpleNamespace(name=name, required=required)


def _ctx(pipeline, arguments, arg_defs, run_id=None):
    cmd_config = SimpleNamespace(arguments=arg_defs, pipeline=pipeline)
    raw_request = SimpleNamespace(arguments=arguments)
    return SimpleNamespace(cmd_config=cmd_config, raw_request=raw_request, run_id=run_id)


def test_build_resolves_placeholders():
    pipeline = [SimpleNamespace(command=["ls", "{dir}"])]
    ctx = _ctx(pipeline, {"dir": "/tmp"}, [_arg("dir")])
    assert PipelineBuilder().build(ctx) == [["ls", "/tmp"]]


def test_build_injects_run_id():
    pipeline = [SimpleNamespace(command=["run", "{run_id}"])]
    ctx = _ctx(pipeline, {}, [], run_id="abc123")
    assert PipelineBuilder().build(ctx) == [["run", "abc123"]]


def test_build_strips_omitted_optional_with_preceding_flag():
    pipeline = [SimpleNamespace(command=["cmd", "--limit", "{limit}"])]
    ctx = _ctx(pipeline, {"limit": None}, [_arg("limit", required=False)])
    assert PipelineBuilder().build(ctx) == [["cmd"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_pipeline_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.pipeline_builder`.

- [ ] **Step 3: Create the module**

Create `app/services/pipeline_builder.py` by moving the three pure methods verbatim (converting `self._resolve_command_part` / `self._strip_omitted_optionals` internal calls to the new method names). Use the exact bodies currently at lines 507-567 of `command_service.py`:

```python
from typing import Any, Dict, List, Optional

from app.domain.command import CommandArgumentConfig, ExecutionContext


class PipelineBuilder:
    """Pure, I/O-free assembly of the final command pipeline from a context.

    Produces ``List[List[str]]`` (e.g. ``[["ls", "-al"], ["grep", "ssh"]]``)
    with no side-effects, making it trivially unit-testable in isolation.
    """

    def resolve_part(
        self, part: str, arguments: Dict[str, Any],
        arg_defs: List[CommandArgumentConfig], run_id: Optional[str] = None,
    ) -> str:
        """Replace {placeholder} tokens in a single command part.

        User-argument placeholders come from ``arguments``/``arg_defs``.
        ``{run_id}`` is server-injected (never a user argument).
        """
        for arg in arg_defs:
            placeholder = f"{{{arg.name}}}"
            if placeholder in part:
                part = part.replace(placeholder, str(arguments[arg.name]))
        if run_id is not None and "{run_id}" in part:
            part = part.replace("{run_id}", run_id)
        return part

    def strip_omitted_optionals(
        self, command: List[str], arguments: Dict[str, Any],
        arg_defs: List[CommandArgumentConfig],
    ) -> List[str]:
        """Remove pipeline tokens for optional args that weren't supplied.

        For each optional (``required=False``) arg the request omitted, drop the
        token containing its ``{name}`` placeholder AND the flag token directly
        before it (so ``["--limit", "{limit}"]`` disappears entirely).
        """
        omitted = {
            arg.name for arg in arg_defs
            if not arg.required and arguments.get(arg.name) is None
        }
        if not omitted:
            return command
        omitted_placeholders = {f"{{{name}}}" for name in omitted}

        drop = set()
        for i, tok in enumerate(command):
            if any(ph in tok for ph in omitted_placeholders):
                drop.add(i)
                if i > 0 and command[i - 1].startswith("-") and "{" not in command[i - 1]:
                    drop.add(i - 1)
        return [tok for i, tok in enumerate(command) if i not in drop]

    def build(self, context: ExecutionContext) -> List[List[str]]:
        """Resolve all {placeholder} tokens and return the final pipeline."""
        args = context.raw_request.arguments
        arg_defs = context.cmd_config.arguments
        return [
            [
                self.resolve_part(part, args, arg_defs, run_id=context.run_id)
                for part in self.strip_omitted_optionals(step.command, args, arg_defs)
            ]
            for step in context.cmd_config.pipeline
        ]
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_pipeline_builder.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Delete the moved methods and delegate from CommandService**

In `command_service.py`:
1. Add the import near the other service imports: `from app.services.pipeline_builder import PipelineBuilder`.
2. In `__init__`, add `self._pipeline_builder = PipelineBuilder()` (after `self.inventory_repo = inventory_repo`).
3. Delete `_resolve_command_part` (507-520), `_strip_omitted_optionals` (522-547), and `_build_pipeline` (549-567).
4. At the one call site in `execute_command` (line 1021), replace `context.pipeline_cmds = self._build_pipeline(context)` with `context.pipeline_cmds = self._pipeline_builder.build(context)`.

- [ ] **Step 6: Verify no stale references**

Run: `grep -n "_build_pipeline\|_resolve_command_part\|_strip_omitted_optionals" app/services/command_service.py`
Expected: no matches.

- [ ] **Step 7: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (baseline + 3 new). If any existing test referenced `svc._build_pipeline` directly, repoint it to `svc._pipeline_builder.build` (grep the tests dir to confirm: `grep -rn "_build_pipeline\|_resolve_command_part\|_strip_omitted_optionals" tests/`).

- [ ] **Step 8: Commit**

```bash
git add app/services/command_service.py app/services/pipeline_builder.py tests/unit/test_pipeline_builder.py
git commit -m "refactor(command): extract PipelineBuilder as pure I/O-free class

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Add observability to silent returns in `kill_command`

**Files:**
- Modify: `app/services/command_service.py` (`kill_command` 1053-1121, `_do_kill_via_connection` already logs)

**Interfaces:**
- No new surface. Control flow unchanged — log lines only.

- [ ] **Step 1: Add a log line to the unknown-command early return**

In `kill_command`, the non-force branch fetches killability from Redis and silently returns on `CommandExecutionException` (currently ~1058-1061):

```python
                try:
                    is_killable = (await self.repo.get(command_id)).killable
                except CommandExecutionException:
                    return
```

Add a log before the return:

```python
                try:
                    is_killable = (await self.repo.get(command_id)).killable
                except CommandExecutionException:
                    logger.info(
                        f"Kill request for unknown command {command_id}; nothing to do.",
                        extra={"command_id": command_id},
                    )
                    return
```

- [ ] **Step 2: Add a log line to the cross-pod state-fetch return**

The cross-pod path fetches state and silently returns on miss (currently ~1090-1093):

```python
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException:
            return
```

Replace with:

```python
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException:
            logger.info(
                f"Cross-pod kill for {command_id} aborted; state vanished from Redis.",
                extra={"command_id": command_id},
            )
            return
```

- [ ] **Step 3: Verify the cross-pod kill failure already logs**

Confirm the `except Exception as e:` at the end of `kill_command` (currently ~1120-1121) already logs:

```python
        except Exception as e:
            logger.error(f"Failed cross-pod kill for {command_id}: {e}", extra={"command_id": command_id})
```

No change needed — this one is already observable. (Documented here so the reviewer knows it was checked, not missed.)

- [ ] **Step 4: Run the kill-path tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_kill_state.py tests/unit/test_command_kill_api.py -v`
Expected: PASS — control flow is unchanged, only log lines added.

- [ ] **Step 5: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_service.py
git commit -m "refactor(command): log silent returns in kill_command for traceability

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Wave 2 (OUT OF SCOPE for this plan)

The collaborator split (`CommandExecutor` / `CommandLifecycle` / `CommandTrace`) is
deferred per the design's optional second wave. It requires its own brainstorming pass
to settle collaborator interfaces and ownership of the module-global pool. Do not start
it as part of this plan. Revisit after Wave 1 lands green and the user confirms.

## Final Verification (after all six tasks)

- [ ] Run the full non-e2e suite once more: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'` — all green.
- [ ] `grep -n "_local_running_commands" app/services/command_service.py` — only declaration + helper bodies.
- [ ] Confirm `git log --oneline develop..feat/command-service-clean-code` shows the spec commit + six task commits.
