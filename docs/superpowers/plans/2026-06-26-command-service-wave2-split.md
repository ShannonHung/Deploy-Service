# CommandService Wave 2 — Collaborator Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the ~1138-line `CommandService` God Class into focused collaborators (pool / ssh / state / executor / lifecycle / trace) behind a thin facade, with zero behavioural change.

**Architecture:** Bottom-up extraction. First move the process-global pool to a neutral module, then extract two shared support collaborators (`SshSupport`, `StateHelpers`), then the three concern collaborators (`CommandTrace`, `CommandLifecycle`, `CommandExecutor`). `CommandService` becomes a thin facade that composes them and delegates its public methods. Method bodies move **verbatim**; only call wiring (`self._x` → `self._collaborator._x`, pool access → `command_pool.*`) changes. The `command_service` module re-exports moved free functions so existing test imports keep resolving.

**Tech Stack:** Python 3, FastAPI, asyncssh, pytest (`asyncio_mode=auto`), uv.

## Global Constraints

- All work on branch `feat/command-service-clean-code` (continues from Wave 1).
- Run git from `deploy-service/` — repo root is `deploy-service/.git`, NOT the `antigravity-fastapi/` top-level dir.
- **No behavioural change.** Every task is a pure move/refactor.
- **No DI restructuring.** `get_command_service` (`app/core/dependencies.py`) stays a per-request factory; routers keep depending on `CommandService`.
- `_local_running_commands` + the execution semaphore stay **module-level** (process-scoped on purpose) — moved to `command_pool.py`, never onto a collaborator `self`.
- The anti-injection architecture (`shlex`-positional args, `_validate_anti_injection`, regex/blacklist) is **not modified** — it moves verbatim with `CommandExecutor`.
- **Backward-compatible module surface:** `command_service` must keep exporting `CommandService`, `CommandExecutionException`, `_decode`, and `pool_add`/`pool_get`/`pool_remove`/`pool_size`/`pool_command_ids`/`_get_semaphore` (via re-export) so existing tests resolve unchanged.
- **Each task ends green:** `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'` must pass before the task is complete. One focused commit per task.
- Single-file test run: `APP_ENV=test uv run pytest tests/unit/<file>.py -v`.

## File Structure

- **Create** `app/services/command_pool.py` (Task 1) — process-global running pool + semaphore + accessors.
- **Create** `app/services/command_ssh.py` (Task 2) — `SshSupport`: SSH connect + ssh-config load.
- **Create** `app/services/command_state_helpers.py` (Task 3) — `StateHelpers`: state load + orphan-heal + exit marker.
- **Create** `app/services/command_trace.py` (Task 4) — `CommandTrace`: remote-log tail + trace response.
- **Create** `app/services/command_lifecycle.py` (Task 5) — `CommandLifecycle`: kill / list / shutdown.
- **Create** `app/services/command_executor.py` (Task 6) — `CommandExecutor`: prepare / connect / run / output / result / capacity / execute_command.
- **Modify** `app/services/command_service.py` (every task) — shrinks to a thin facade by the end.

No test files need editing (re-exports preserve all current imports/usages). New unit tests are added only where a collaborator gains an independent seam (Task 3).

---

### Task 1: Extract `command_pool.py` (process-global running pool)

**Files:**
- Create: `app/services/command_pool.py`
- Modify: `app/services/command_service.py` (remove pool global + `pool_*` + semaphore at lines 41–76; add import + re-export)

**Interfaces:**
- Produces (module-level in `app.services.command_pool`):
  - `pool_add(command_id: str, entry: RunningCommandEntry) -> None`
  - `pool_get(command_id: str) -> Optional[RunningCommandEntry]`
  - `pool_remove(command_id: str) -> None`
  - `pool_size() -> int`
  - `pool_command_ids() -> List[str]`
  - `_get_semaphore() -> asyncio.Semaphore`
- Consumes: `RunningCommandEntry` (`app.domain.command`), `get_settings` (`app.core.config`).

- [ ] **Step 1: Create the new module**

Create `app/services/command_pool.py` by moving lines 41–76 of `command_service.py` verbatim (the `_local_running_commands` global, the five `pool_*` functions, `_execution_semaphore`, and `_get_semaphore`), with the imports they need:

```python
import asyncio
from typing import Dict, List, Optional

from app.domain.command import RunningCommandEntry
from app.core.config import get_settings

settings = get_settings()

_local_running_commands: Dict[str, RunningCommandEntry] = {}


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


_execution_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Lazily initialise the concurrency semaphore (must be called inside a running event loop)."""
    global _execution_semaphore
    if _execution_semaphore is None:
        _execution_semaphore = asyncio.Semaphore(settings.COMMAND_MAX_CONCURRENCY)
    return _execution_semaphore
```

- [ ] **Step 2: Replace the moved block in `command_service.py` with an import + re-export**

Delete lines 41–76 of `command_service.py` (the `_local_running_commands` global through the end of `_get_semaphore`). In their place, add a re-export so existing references (`cs.pool_add`, `cs._get_semaphore`, and internal callers) keep resolving:

```python
from app.services.command_pool import (  # noqa: F401  (re-exported for callers/tests)
    pool_add, pool_get, pool_remove, pool_size, pool_command_ids,
    _get_semaphore,
)
```

Leave `_decode` (lines 79–85) where it is for now. The `logger` and `settings` module globals (38–39) stay.

- [ ] **Step 3: Verify no direct global access remains in `command_service.py`**

Run: `grep -n "_local_running_commands\|_execution_semaphore" app/services/command_service.py`
Expected: no matches (all access is now via the re-exported `pool_*` / `_get_semaphore`).

- [ ] **Step 4: Run the coupled pool tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_kill_state.py tests/unit/test_command_detached_spawn.py tests/unit/test_command_service_errors.py -v`
Expected: PASS — these call `cs.pool_add` / `cs.pool_remove`, which still resolve via the re-export.

- [ ] **Step 5: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as Wave 1 baseline).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_pool.py app/services/command_service.py
git commit -m "refactor(command): extract command_pool module for process-global running pool

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Extract `SshSupport` (SSH connect + ssh-config load)

**Files:**
- Create: `app/services/command_ssh.py`
- Modify: `app/services/command_service.py` (remove `_load_ssh_config` 131–150 and `_connect_to_control_node` 198–230; add `self._ssh` in `__init__`; repoint internal callers)

**Interfaces:**
- Produces: `class SshSupport` with
  - `def _load_ssh_config(self, target: str) -> SSHConnectionConfig`
  - `async def _connect_to_control_node(self, state: CommandState) -> asyncssh.SSHClientConnection`
- Consumes: `create_authenticator` (`app.repositories.ssh_auth_repository`), `SSHConnectionConfig` / `CommandState` (`app.domain.command`), `settings`, the upstream exceptions, `os`, `json`, `asyncio`, `asyncssh`.

- [ ] **Step 1: Create `command_ssh.py`**

Create `app/services/command_ssh.py`. Move the bodies of `_load_ssh_config` (currently 131–150) and `_connect_to_control_node` (currently 198–230) **verbatim** into a `SshSupport` class (same method names, same `self.` receiver — `_connect_to_control_node` already calls `self._load_ssh_config`, which now lives on the same class, so no rewiring inside):

```python
import asyncio
import json
import os
import logging

import asyncssh

from app.domain.command import SSHConnectionConfig, CommandState
from app.core.config import get_settings
from app.repositories.ssh_auth_repository import create_authenticator
from app.core.exceptions import (
    UpstreamTimeoutException,
    UpstreamUnavailableException,
    BaseAppException,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class SshSupport:
    """SSH connection + config loading shared by executor, lifecycle, trace, poll."""

    def _load_ssh_config(self, target: str) -> SSHConnectionConfig:
        # <body verbatim from command_service.py lines 132-150>
        ...

    async def _connect_to_control_node(self, state: CommandState) -> asyncssh.SSHClientConnection:
        # <body verbatim from command_service.py lines 199-230>
        ...
```

Copy the exact docstrings and bodies from the current source (lines 131–150 and 198–230). Do not alter logic.

- [ ] **Step 2: Wire `SshSupport` into the facade and repoint callers**

In `command_service.py`:
1. Add import: `from app.services.command_ssh import SshSupport`.
2. In `__init__` (after `self._pipeline_builder = PipelineBuilder()`), add: `self._ssh = SshSupport()`.
3. Delete `_load_ssh_config` (131–150) and `_connect_to_control_node` (198–230) from the class.
4. Repoint the three remaining callers in `command_service.py`:
   - `_read_run_exit_marker` (line 252): `conn = await self._connect_to_control_node(state)` → `conn = await self._ssh._connect_to_control_node(state)`
   - `_read_remote_log` (line 334): `conn = await self._connect_to_control_node(state)` → `conn = await self._ssh._connect_to_control_node(state)`
   - `kill_command` (line 1086): `ssh_config = self._load_ssh_config(state.ssh_config)` → `ssh_config = self._ssh._load_ssh_config(state.ssh_config)`
   - `_connect` (line ~547, the execute path): check whether it calls `self._load_ssh_config` — if so, repoint to `self._ssh._load_ssh_config`. (Grep in Step 3 confirms.)

- [ ] **Step 3: Verify no stale references**

Run: `grep -n "self\._load_ssh_config\|self\._connect_to_control_node\|def _load_ssh_config\|def _connect_to_control_node" app/services/command_service.py`
Expected: no matches (all calls go through `self._ssh.`, and the method defs are gone).

- [ ] **Step 4: Run the affected tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_trace.py tests/unit/test_command_orphan_heal.py tests/unit/test_command_kill_state.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_ssh.py app/services/command_service.py
git commit -m "refactor(command): extract SshSupport for shared SSH connect/config

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Extract `StateHelpers` (state load + orphan-heal + exit marker)

**Files:**
- Create: `app/services/command_state_helpers.py`
- Modify: `app/services/command_service.py` (remove `_get_state_or_404` 152–163, `_exit_marker_path` 232–236, `_read_run_exit_marker` 238–269, `_heal_from_marker` 271–320; add `self._state` in `__init__`; repoint callers)
- Create: `tests/unit/test_command_state_helpers.py`

**Interfaces:**
- Produces: `class StateHelpers` constructed as `StateHelpers(repo: CommandStateRepository, ssh: SshSupport)` with
  - `async def _get_state_or_404(self, command_id: str) -> CommandState`
  - `def _exit_marker_path(self, run_log_path: str) -> str`
  - `async def _read_run_exit_marker(self, state: CommandState) -> Optional[int]`
  - `async def _heal_from_marker(self, state: CommandState) -> CommandState`
- Consumes: `self.repo` (was `CommandService.repo`), `self._ssh._connect_to_control_node` (was `self._connect_to_control_node`), `SshSupport`.

- [ ] **Step 1: Write a failing unit test for the new seam**

Create `tests/unit/test_command_state_helpers.py` exercising `_exit_marker_path` (pure) and `_heal_from_marker`'s no-marker passthrough (heal logic in isolation):

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.command_state_helpers import StateHelpers
from app.domain.command import CommandState, CommandStatus


def _helpers(repo=None, ssh=None):
    return StateHelpers(repo=repo or MagicMock(), ssh=ssh or MagicMock())


def test_exit_marker_path_swaps_log_suffix():
    h = _helpers()
    assert h._exit_marker_path("/runs/abc.log") == "/runs/abc.exit"


def test_exit_marker_path_appends_when_no_log_suffix():
    h = _helpers()
    assert h._exit_marker_path("/runs/abc") == "/runs/abc.exit"


async def test_heal_returns_state_unchanged_when_no_marker(monkeypatch):
    h = _helpers()
    state = CommandState(
        command_id="c1", status=CommandStatus.RUNNING, run_log_path="/runs/c1.log",
    )
    # No marker yet -> _read_run_exit_marker returns None -> passthrough.
    h._read_run_exit_marker = AsyncMock(return_value=None)
    result = await h._heal_from_marker(state)
    assert result is state
```

(If `CommandState` requires more constructor fields, mirror the construction used in `tests/unit/test_command_orphan_heal.py`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_state_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.command_state_helpers`.

- [ ] **Step 3: Create `command_state_helpers.py`**

Create `app/services/command_state_helpers.py`. Move `_get_state_or_404` (152–163), `_exit_marker_path` (232–236), `_read_run_exit_marker` (238–269), and `_heal_from_marker` (271–320) **verbatim** into a `StateHelpers` class. Two rewirings inside the moved bodies:
- In `_read_run_exit_marker`: `conn = await self._connect_to_control_node(state)` → `conn = await self._ssh._connect_to_control_node(state)`.
- `_heal_from_marker` and `_get_state_or_404` already use `self.repo` — keep as-is (the class holds `self.repo`).

```python
import logging
import shlex
from typing import Optional

from app.domain.command import CommandState, CommandStatus
from app.core.config import get_settings
from app.repositories.command_state_repository import CommandStateRepository
from app.services.command_ssh import SshSupport
from app.core.exceptions import (
    CommandExecutionException, NotFoundException, BaseAppException,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class StateHelpers:
    """Redis state load + lazy orphan-run heal from the control_node exit marker."""

    def __init__(self, repo: CommandStateRepository, ssh: SshSupport):
        self.repo = repo
        self._ssh = ssh

    async def _get_state_or_404(self, command_id: str) -> CommandState:
        # <body verbatim from lines 157-163>
        ...

    def _exit_marker_path(self, run_log_path: str) -> str:
        # <body verbatim from lines 234-236>
        ...

    async def _read_run_exit_marker(self, state: CommandState) -> Optional[int]:
        # <body verbatim from lines 251-269, with _connect_to_control_node -> self._ssh._connect_to_control_node>
        ...

    async def _heal_from_marker(self, state: CommandState) -> CommandState:
        # <body verbatim from lines 283-320>
        ...
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_state_helpers.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire `StateHelpers` into the facade and repoint callers**

In `command_service.py`:
1. Add import: `from app.services.command_state_helpers import StateHelpers`.
2. In `__init__`, after `self._ssh = SshSupport()`, add: `self._state = StateHelpers(repo=self.repo, ssh=self._ssh)`.
3. Delete the four moved methods (152–163, 232–236, 238–269, 271–320) from the class.
4. Repoint callers in `command_service.py`:
   - `get_command_execution_result` (line 178): `state = await self._get_state_or_404(command_id)` → `state = await self._state._get_state_or_404(command_id)`
   - `get_command_execution_result` (line 184): `state = await self._heal_from_marker(state)` → `state = await self._state._heal_from_marker(state)`
   - `get_command_trace` (line 362): `state = await self._get_state_or_404(command_id)` → `state = await self._state._get_state_or_404(command_id)`

- [ ] **Step 6: Verify no stale references**

Run: `grep -n "self\._get_state_or_404\|self\._heal_from_marker\|self\._read_run_exit_marker\|self\._exit_marker_path\|def _get_state_or_404\|def _heal_from_marker\|def _read_run_exit_marker\|def _exit_marker_path" app/services/command_service.py`
Expected: no matches.

- [ ] **Step 7: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (baseline + 3 new). `test_command_orphan_heal.py` must still pass (it exercises heal via the facade).

- [ ] **Step 8: Commit**

```bash
git add app/services/command_state_helpers.py app/services/command_service.py tests/unit/test_command_state_helpers.py
git commit -m "refactor(command): extract StateHelpers for state load + orphan heal

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Extract `CommandTrace` (remote-log tail + trace response)

**Files:**
- Create: `app/services/command_trace.py`
- Modify: `app/services/command_service.py` (remove `_read_remote_log` 322–350 and `get_command_trace` 352–416; add `self._trace`; facade `get_command_trace` delegates)

**Interfaces:**
- Produces: `class CommandTrace` constructed as `CommandTrace(state: StateHelpers, ssh: SshSupport)` with
  - `async def get_command_trace(self, command_id: str, byte_offset: int = 0, line_num: int = 1) -> CommandTraceResponse`
  - `async def _read_remote_log(self, state: CommandState, byte_offset: int) -> tuple[int, str]`
- Consumes: `self._state._get_state_or_404`, `self._ssh._connect_to_control_node`, `LogRenderer`, `CommandLogLine`/`CommandTraceResponse`, `settings`.

- [ ] **Step 1: Create `command_trace.py`**

Create `app/services/command_trace.py`. Move `_read_remote_log` (322–350) and `get_command_trace` (352–416) **verbatim** into a `CommandTrace` class. Rewirings inside the moved bodies:
- In `_read_remote_log`: `conn = await self._connect_to_control_node(state)` → `conn = await self._ssh._connect_to_control_node(state)`.
- In `get_command_trace`: `state = await self._get_state_or_404(command_id)` → `state = await self._state._get_state_or_404(command_id)`; `total_size, new_text = await self._read_remote_log(...)` stays (same class).

```python
import logging
import shlex

from app.domain.command import CommandState, CommandLogLine, CommandTraceResponse
from app.core.config import get_settings
from app.core.log_renderer import LogRenderer
from app.services.command_ssh import SshSupport
from app.services.command_state_helpers import StateHelpers

logger = logging.getLogger(__name__)
settings = get_settings()


class CommandTrace:
    """Incremental remote-log tail rendered for the UI log viewer."""

    def __init__(self, state: StateHelpers, ssh: SshSupport):
        self._state = state
        self._ssh = ssh

    async def _read_remote_log(self, state: CommandState, byte_offset: int) -> tuple[int, str]:
        # <body verbatim from lines 333-350, _connect_to_control_node -> self._ssh._connect_to_control_node>
        ...

    async def get_command_trace(self, command_id: str, byte_offset: int = 0, line_num: int = 1) -> CommandTraceResponse:
        # <body verbatim from lines 362-416, _get_state_or_404 -> self._state._get_state_or_404>
        ...
```

- [ ] **Step 2: Wire into the facade and delegate**

In `command_service.py`:
1. Add import: `from app.services.command_trace import CommandTrace`.
2. In `__init__`, after `self._state = ...`, add: `self._trace = CommandTrace(state=self._state, ssh=self._ssh)`.
3. Delete `_read_remote_log` (322–350) and the body of `get_command_trace` (352–416).
4. Replace `get_command_trace` with a thin delegator:

```python
    async def get_command_trace(self, command_id: str, byte_offset: int = 0, line_num: int = 1) -> CommandTraceResponse:
        return await self._trace.get_command_trace(command_id, byte_offset, line_num)
```

- [ ] **Step 3: Verify no stale references**

Run: `grep -n "self\._read_remote_log\|def _read_remote_log" app/services/command_service.py`
Expected: no matches.

- [ ] **Step 4: Run the trace tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_trace.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_trace.py app/services/command_service.py
git commit -m "refactor(command): extract CommandTrace for log-viewer tail

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Extract `CommandLifecycle` (kill / list / shutdown)

**Files:**
- Create: `app/services/command_lifecycle.py`
- Modify: `app/services/command_service.py` (remove `kill_command` 1003–1101, `_do_kill_via_connection` 1103–1114, `list_running_commands` 1116–1127, `shutdown_gracefully` 1129–end; add `self._lifecycle`; facade delegates)

**Interfaces:**
- Produces: `class CommandLifecycle` constructed as `CommandLifecycle(repo: CommandStateRepository, ssh: SshSupport)` with
  - `async def kill_command(self, command_id: str, message: str = "Killed", force: bool = False)`
  - `async def _do_kill_via_connection(self, conn, pgids: List[int], command_id: str)`
  - `async def list_running_commands(self, statuses: Optional[set[CommandStatus]] = None) -> List[CommandState]`
  - `async def shutdown_gracefully(self)`
- Consumes: `self.repo`, `self._ssh._load_ssh_config`, `command_pool.pool_get` / `pool_size` / `pool_command_ids`, `create_authenticator`, `settings`, exceptions, `asyncio`, `asyncssh`.

- [ ] **Step 1: Create `command_lifecycle.py`**

Create `app/services/command_lifecycle.py`. Move `kill_command` (1003–1101), `_do_kill_via_connection` (1103–1114), `list_running_commands` (1116–1127), `shutdown_gracefully` (1129–end) **verbatim** into a `CommandLifecycle` class. Rewirings inside the moved bodies:
- `kill_command`: `entry = pool_get(command_id)` (lines 1026, 1059) — these resolve via module import of `command_pool` (use `pool_get(...)` after `from app.services.command_pool import pool_get, ...`). `ssh_config = self._load_ssh_config(state.ssh_config)` (line 1086) → `ssh_config = self._ssh._load_ssh_config(state.ssh_config)`.
- `shutdown_gracefully`: `pool_size()` and `pool_command_ids()` resolve via the `command_pool` import; `self.kill_command(...)` stays (same class).
- `list_running_commands`: uses `self.repo` only — unchanged.

```python
import asyncio
import logging
from typing import List, Optional

import asyncssh

from app.domain.command import CommandState, CommandStatus
from app.core.config import get_settings
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.ssh_auth_repository import create_authenticator
from app.services.command_ssh import SshSupport
from app.services.command_pool import pool_get, pool_size, pool_command_ids
from app.core.exceptions import CommandExecutionException

logger = logging.getLogger(__name__)
settings = get_settings()


class CommandLifecycle:
    """Kill / list / graceful-shutdown for running commands (local + cross-pod)."""

    def __init__(self, repo: CommandStateRepository, ssh: SshSupport):
        self.repo = repo
        self._ssh = ssh

    async def kill_command(self, command_id: str, message: str = "Killed", force: bool = False):
        # <body verbatim from lines 1018-1101, _load_ssh_config -> self._ssh._load_ssh_config>
        ...

    async def _do_kill_via_connection(self, conn: asyncssh.SSHClientConnection, pgids: List[int], command_id: str):
        # <body verbatim from lines 1104-1114>
        ...

    async def list_running_commands(self, statuses: Optional[set[CommandStatus]] = None) -> List[CommandState]:
        # <body verbatim from lines 1125-1127>
        ...

    async def shutdown_gracefully(self):
        # <body verbatim from lines 1135-end>
        ...
```

- [ ] **Step 2: Wire into the facade and delegate**

In `command_service.py`:
1. Add import: `from app.services.command_lifecycle import CommandLifecycle`.
2. In `__init__`, after `self._trace = ...`, add: `self._lifecycle = CommandLifecycle(repo=self.repo, ssh=self._ssh)`.
3. Delete the four moved methods.
4. Add thin delegators:

```python
    async def kill_command(self, command_id: str, message: str = "Killed", force: bool = False):
        return await self._lifecycle.kill_command(command_id, message, force)

    async def list_running_commands(self, statuses: Optional[set[CommandStatus]] = None) -> List[CommandState]:
        return await self._lifecycle.list_running_commands(statuses)

    async def shutdown_gracefully(self):
        return await self._lifecycle.shutdown_gracefully()
```

- [ ] **Step 3: Verify no stale references**

Run: `grep -n "self\._do_kill_via_connection\|def _do_kill_via_connection" app/services/command_service.py`
Expected: no matches.

- [ ] **Step 4: Run the kill / lifecycle tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_kill_state.py tests/unit/test_command_kill_api.py tests/unit/test_command_service_running.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_lifecycle.py app/services/command_service.py
git commit -m "refactor(command): extract CommandLifecycle for kill/list/shutdown

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Extract `CommandExecutor` (prepare / run / output / capacity / execute_command)

**Files:**
- Create: `app/services/command_executor.py`
- Modify: `app/services/command_service.py` (remove the execution cluster; add `self._executor`; facade `execute_command` delegates; move `_decode`, `_validate_anti_injection`, `_load_user_whitelist`)

**Interfaces:**
- Produces: `class CommandExecutor` constructed as `CommandExecutor(repo, inventory_repo, ssh: SshSupport)` with
  - `async def execute_command(self, username: str, request_id: str, req: CommandExecutionRequest) -> CommandExecutionResponse`
  - plus the private helpers it owns (`_prepare_execution`, `_connect`, `_build_step_wrapper`, `_execute_pipeline`, `_collect_output`, `_handle_async_execution`, `_handle_fire_and_forget`, `_apply_output_policy`, `_store_result`, `_check_capacity`, `_compute_log_path`, `_validate_anti_injection`, `_load_user_whitelist`, and a `PipelineBuilder` instance).
- Consumes: `self.repo`, `self.inventory_repo`, `self._ssh._load_ssh_config`, `command_pool.pool_add`/`pool_remove`/`pool_size`/`_get_semaphore`, `PipelineBuilder`, `create_host_resolver`/`ResolvedHost`, `_decode`, the domain models, `settings`.

> **Note on `get_user_commands` / `get_command_info`:** these facade methods call `self._load_user_whitelist`. After moving `_load_user_whitelist` onto `CommandExecutor`, repoint them to `self._executor._load_user_whitelist` (Step 4).

- [ ] **Step 1: Identify the exact execution-cluster line ranges**

Run: `grep -nE "    (async )?def (_prepare_execution|_compute_log_path|_connect|_handle_fire_and_forget|_apply_output_policy|_store_result|_build_step_wrapper|_execute_pipeline|_collect_output|_handle_async_execution|_check_capacity|execute_command|_validate_anti_injection|_load_user_whitelist)\b" app/services/command_service.py`
Expected: prints the start line of each method to move. Use these to copy each body verbatim.

- [ ] **Step 2: Create `command_executor.py`**

Create `app/services/command_executor.py`. Move these methods **verbatim** into a `CommandExecutor` class: `_validate_anti_injection`, `_load_user_whitelist`, `_prepare_execution`, `_compute_log_path`, `_connect`, `_handle_fire_and_forget`, `_apply_output_policy`, `_store_result`, `_build_step_wrapper`, `_execute_pipeline`, `_collect_output`, `_handle_async_execution`, `_check_capacity`, `execute_command`. Also move the module-level `_decode` (lines 79–85) here as a module function. Rewirings inside the moved bodies:
- `__init__`: holds `self.repo`, `self.inventory_repo`, `self._ssh`, `self._pipeline_builder = PipelineBuilder()`.
- `_connect` / any `self._load_ssh_config(...)` → `self._ssh._load_ssh_config(...)`.
- `execute_command`: `context.pipeline_cmds = self._pipeline_builder.build(context)` stays; `self._check_capacity(...)`, `self._prepare_execution(...)`, `self._connect(...)`, `self._handle_*` all stay (same class).
- `_check_capacity`: `pool_size()` via `command_pool` import.
- `_handle_async_execution` / wrappers: `pool_add` / `pool_remove` / `_get_semaphore` via `command_pool` import.
- `_collect_output` / `_handle_fire_and_forget`: `_decode(...)` now the local module function.

```python
import asyncio
import logging
import shlex
import uuid
from typing import Any, Dict, List, Optional

import asyncssh

from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    UserCommandWhitelist, SSHConnectionConfig, RunningCommandEntry,
    ExecutionContext, CommandState, CommandStatus, HostType,
    CommandArgumentConfig,
)
from app.core.config import get_settings
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.host_resolver import ResolvedHost, create_host_resolver
from app.services.pipeline_builder import PipelineBuilder
from app.services.command_ssh import SshSupport
from app.services.command_pool import pool_add, pool_remove, pool_size, _get_semaphore
from app.core.exceptions import (
    CommandExecutionException, UpstreamTimeoutException, UpstreamUnavailableException,
    ForbiddenException, ServiceUnavailableException, BaseAppException,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _decode(stream: Any) -> str:
    # <body verbatim from command_service.py lines 80-85>
    ...


class CommandExecutor:
    """Validate, resolve, connect, run, collect output, and store results."""

    def __init__(self, repo: CommandStateRepository, inventory_repo: Optional[InventoryRepository], ssh: SshSupport):
        self.repo = repo
        self.inventory_repo = inventory_repo
        self._ssh = ssh
        self._pipeline_builder = PipelineBuilder()

    # ... all moved methods verbatim, with the rewirings listed above ...
```

> Copy each method body exactly from the current source (use the line numbers from Step 1). Verify the import list against what the moved bodies actually reference — add any name the grep in Step 5 reports as undefined.

- [ ] **Step 3: Keep a `_decode` re-export in `command_service.py`**

`tests/unit/test_command_decode.py` does `from app.services.command_service import _decode`. Since `_decode` now lives in `command_executor.py`, add to `command_service.py`:

```python
from app.services.command_executor import CommandExecutor, _decode  # noqa: F401  (_decode re-exported for tests)
```

- [ ] **Step 4: Wire into the facade, delegate, repoint whitelist callers**

In `command_service.py`:
1. In `__init__`, after `self._lifecycle = ...`, add: `self._executor = CommandExecutor(repo=self.repo, inventory_repo=self.inventory_repo, ssh=self._ssh)`. Remove the now-unused `self._pipeline_builder = PipelineBuilder()` from the facade (the executor owns its own).
2. Delete all moved methods + the old module-level `_decode` body from `command_service.py`.
3. Replace `execute_command` with a delegator:

```python
    async def execute_command(self, username: str, request_id: str, req: CommandExecutionRequest) -> CommandExecutionResponse:
        return await self._executor.execute_command(username, request_id, req)
```

4. Repoint `get_user_commands` and `get_command_info` to the executor's whitelist loader:

```python
    def get_user_commands(self, username: str) -> UserCommandWhitelist:
        return self._executor._load_user_whitelist(username)

    def get_command_info(self, username: str, command_name: str) -> CommandWhitelistConfig:
        whitelist = self._executor._load_user_whitelist(username)
        cmd_config = next((c for c in whitelist.allow_commands if c.command_name == command_name), None)
        if not cmd_config:
            raise CommandExecutionException(f"Command '{command_name}' not found.")
        return cmd_config
```

- [ ] **Step 5: Verify no stale references**

Run: `grep -nE "self\._(prepare_execution|connect|handle_|apply_output_policy|store_result|build_step_wrapper|execute_pipeline|collect_output|check_capacity|validate_anti_injection|load_user_whitelist|pipeline_builder|compute_log_path)" app/services/command_service.py`
Expected: matches ONLY inside the two whitelist delegators (`self._executor._load_user_whitelist`) — no bare `self._<moved>` calls remain.

- [ ] **Step 6: Run the execution + anti-injection + capacity tests**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_service.py tests/unit/test_command_service_errors.py tests/unit/test_command_detached_spawn.py tests/unit/test_command_output_policy.py tests/unit/test_command_run_id_injection.py tests/unit/test_optional_arguments.py tests/unit/test_command_decode.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (same count as baseline).

- [ ] **Step 8: Commit**

```bash
git add app/services/command_executor.py app/services/command_service.py
git commit -m "refactor(command): extract CommandExecutor for the execution pipeline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Slim the facade + final verification

**Files:**
- Modify: `app/services/command_service.py` (cleanup only — remove dead imports, confirm thinness)

**Interfaces:**
- No new surface. `CommandService` is now: `__init__` (wires 5 collaborators) + delegators (`get_command_execution_result`, `get_command_trace`, `execute_command`, `kill_command`, `list_running_commands`, `shutdown_gracefully`, `get_user_commands`, `get_command_info`) + the re-export lines.

- [ ] **Step 1: Confirm the facade is thin**

Run: `grep -nE "^\s*(async )?def " app/services/command_service.py`
Expected: only `__init__` and the eight public methods listed above — no private `_`-prefixed methods remain (except none).

- [ ] **Step 2: Prune now-unused imports in `command_service.py`**

Remove imports no longer referenced by the facade (e.g. `asyncssh`, `shlex`, `re`, `os`, `json`, `create_authenticator`, `LogRenderer`, host-resolver imports, `PipelineBuilder` direct import if unused). Keep: the domain models used in delegator signatures (`CommandExecutionRequest`, `CommandExecutionResponse`, `UserCommandWhitelist`, `CommandWhitelistConfig`, `CommandState`, `CommandStatus`, `CommandTraceResponse`), `CommandStateRepository`, `InventoryRepository`, the collaborator imports, the re-exports (`pool_*`, `_get_semaphore`, `_decode`), and `CommandExecutionException` (re-exported + used in `get_command_info`).

Verify with: `APP_ENV=test uv run python -c "import app.services.command_service"`
Expected: no ImportError / NameError.

- [ ] **Step 3: Confirm backward-compatible surface still resolves**

Run:
```bash
APP_ENV=test uv run python -c "from app.services.command_service import CommandService, CommandExecutionException, _decode, pool_add, pool_get, pool_remove, _get_semaphore; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 4: Final grep — no stale cross-references**

Run: `grep -rn "_local_running_commands\|_execution_semaphore" app/services/command_service.py`
Expected: no matches.
Run: `grep -c "def " app/services/command_service.py`
Expected: 9 (init + 8 delegators).

- [ ] **Step 5: Run the full non-e2e suite**

Run: `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'`
Expected: PASS (Wave 1 baseline count + 3 new from Task 3).

- [ ] **Step 6: Confirm line count dropped**

Run: `wc -l app/services/command_service.py app/services/command_*.py`
Expected: `command_service.py` is now a few hundred lines (facade only); the rest distributed across the new collaborator modules.

- [ ] **Step 7: Commit**

```bash
git add app/services/command_service.py
git commit -m "refactor(command): slim CommandService to a thin facade over collaborators

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final Verification (after all seven tasks)

- [ ] `APP_ENV=test uv run pytest tests/ -v -m 'not e2e'` — all green.
- [ ] `grep -rn "_local_running_commands" app/services/` — only in `command_pool.py`.
- [ ] `git log --oneline develop..feat/command-service-clean-code` — Wave 1 commits + the Wave 2 spec commit + seven task commits.
- [ ] Facade `command_service.py` contains only `__init__` + 8 delegators + re-exports.
