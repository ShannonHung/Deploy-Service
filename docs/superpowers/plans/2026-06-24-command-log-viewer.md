# Command Execution Log Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give long-running ansible commands a live, auto-refreshing log viewer (like the deploy job view) by tailing a per-run log file on the control_node over SSH, while `/execution/{id}` keeps reporting only success/failure.

**Architecture:** `run-ansible.sh` tees its output to `<log-dir>/<run_id>.log` on the control_node. deploy-service stores the log path on the Redis `CommandState`, then serves an incremental byte-offset trace endpoint (`tail -c +N` over SSH) and a reused HTML viewer. Stateless offset polling — no long-lived `tail -f` channel.

**Tech Stack:** FastAPI, asyncssh, Redis (CommandStateRepository), pytest (`asyncio_mode=auto`), uv. Bash for `run-ansible.sh`.

## Global Constraints

- Working directory for all commands: `deploy-service/`.
- Run tests with: `APP_ENV=test uv run pytest <path> -v`.
- Anti-injection invariant (load-bearing): user/server values are passed as **discrete positional args** through `setsid -w sh -c 'echo $$ >&2; exec "$@"' _ <cmd> <args...>` and quoted with `shlex.join`. Never interpolate a value into a shell string. The new `run_log_path` and `run_id` MUST flow as discrete args, never spliced into a shell command.
- Layering: routers → services → repository interfaces. Services never import asyncssh-construction logic except via the existing `_connect` / `create_authenticator` patterns already in `command_service.py`.
- Unified response: routes return `ApiResponse[T]` with `request_id` from `_request_id(request)`.
- `get_settings()` is `lru_cache`'d; tests reset with `get_settings.cache_clear()`.
- No new external dependency. Reuse `LogRenderer` (`app/core/log_renderer.py`) and `LOG_VIEWER_HTML` (`app/core/log_viewer_template.py`).

---

## File Structure

- `app/domain/command.py` — add `run_log_path` to `CommandState`; add `logged` to `CommandWhitelistConfig`; add `CommandLogLine` + `CommandTraceResponse`.
- `app/services/command_service.py` — inject `run_id`/`run_log_path` for `logged` commands; add `get_command_trace(...)` + a private SSH file-read helper.
- `app/core/config.py` — add `COMMAND_LOG_DIR`, `COMMAND_LOG_SOFT_CAP_BYTES`, `COMMAND_LOG_HARD_CAP_BYTES`.
- `app/core/log_viewer_template.py` — parameterise the fetch URL + labels (shared by deploy + command viewers).
- `app/api/v1/command.py` — add `GET /execution/{id}/trace/ui` and `GET /execution/{id}/view`.
- `app/api/v1/deploy.py` — update `view_job` to pass the new template slots (keep behaviour identical).
- `ansible/run-ansible.sh` — `--run-id` → tee to `<log-dir>/<run_id>.log`; `--log-retention-days` (default 3) self-cleaning prune at run start. (Already implemented.)
- `data/allow-commands-admin.json`, `data/allow-commands-test_admin.json` — set `logged: true` and add `--log-dir`/`--run-id {run_id}` to `run_ansible_*` entries.
- Tests under `tests/unit/` and `tests/integration/`.

---

### Task 1: Domain models — log pointer, opt-in flag, trace response

**Files:**
- Modify: `app/domain/command.py`
- Test: `tests/unit/test_command_models.py`

**Interfaces:**
- Produces:
  - `CommandState.run_log_path: Optional[str] = None`
  - `CommandWhitelistConfig.logged: bool = False`
  - `CommandLogLine(num: int, content_html: str)`
  - `CommandTraceResponse(command_id: str, status: str, next_byte_offset: int, next_line_num: int, lines: list[CommandLogLine], total_size: int = 0, size_warning: bool = False, too_large: bool = False)`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_models.py`:

```python
from app.domain.command import (
    CommandState, CommandStatus, CommandWhitelistConfig,
    PipelineStep, CommandLogLine, CommandTraceResponse,
)


def test_command_state_run_log_path_defaults_none():
    state = CommandState(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=22, username="root", ssh_config="default",
        request_id="r1", exec_command="echo hi", killable=True,
    )
    assert state.run_log_path is None
    state.run_log_path = "/var/log/ansible-runs/c1.log"
    assert state.run_log_path.endswith("c1.log")


def test_whitelist_logged_defaults_false():
    cfg = CommandWhitelistConfig(
        command_name="x", pipeline=[PipelineStep(command=["echo", "hi"])],
    )
    assert cfg.logged is False
    cfg2 = CommandWhitelistConfig(
        command_name="y", logged=True,
        pipeline=[PipelineStep(command=["echo", "hi"])],
    )
    assert cfg2.logged is True


def test_command_trace_response_shape():
    resp = CommandTraceResponse(
        command_id="c1", status="running", next_byte_offset=10,
        next_line_num=3, lines=[CommandLogLine(num=1, content_html="<span>hi</span>")],
    )
    assert resp.total_size == 0
    assert resp.size_warning is False
    assert resp.too_large is False
    assert resp.lines[0].num == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'CommandLogLine'` (and `run_log_path`/`logged` attribute errors).

- [ ] **Step 3: Write minimal implementation**

In `app/domain/command.py`, add `run_log_path: Optional[str] = None` to `CommandState` (alongside the other optional fields near `output`/`exit_code`):

```python
class CommandState(BaseModel):
    command_id: str
    status: CommandStatus
    output: Optional[str] = None
    exit_code: Optional[int] = None
    message: Optional[str] = None
    run_log_path: Optional[str] = None  # control_node path of the tee'd run log
    # ... existing metadata/control fields unchanged ...
```

Add `logged` to `CommandWhitelistConfig`:

```python
class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    logged: bool = False  # opt-in: tee output to a per-run file + expose viewer
    pipeline: List[PipelineStep]
    arguments: List[CommandArgumentConfig] = []
```

At the end of the "Request / Response" section, add:

```python
class CommandLogLine(BaseModel):
    num: int
    content_html: str


class CommandTraceResponse(BaseModel):
    """Incremental slice of processed command-log lines for the UI.

    Mirrors the deploy FormattedLogResponse but keyed by command_id and
    carrying the command's lifecycle status.
    """
    command_id: str
    status: str
    next_byte_offset: int
    next_line_num: int
    lines: List[CommandLogLine]
    total_size: int = 0
    size_warning: bool = False
    too_large: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_models.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/domain/command.py tests/unit/test_command_models.py
git commit -m "feat(command): add run_log_path, logged flag, and trace response models"
```

---

### Task 2: Settings — command log dir, size caps, failure-tail

**Files:**
- Modify: `app/core/config.py`
- Test: `tests/unit/test_command_log_settings.py`

**Interfaces:**
- Produces: `settings.COMMAND_LOG_DIR: str`, `settings.COMMAND_LOG_SOFT_CAP_BYTES: int`, `settings.COMMAND_LOG_HARD_CAP_BYTES: int`, `settings.COMMAND_LOG_FAILURE_TAIL_LINES: int`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_log_settings.py`:

```python
from app.core.config import get_settings


def test_command_log_settings_present():
    get_settings.cache_clear()
    s = get_settings()
    assert s.COMMAND_LOG_DIR == "/var/log/ansible-runs"
    assert s.COMMAND_LOG_SOFT_CAP_BYTES == 5 * 1024 * 1024
    assert s.COMMAND_LOG_HARD_CAP_BYTES == 10 * 1024 * 1024
    assert s.COMMAND_LOG_FAILURE_TAIL_LINES == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_log_settings.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'COMMAND_LOG_DIR'`.

- [ ] **Step 3: Write minimal implementation**

In `app/core/config.py`, next to the existing `COMMAND_*` settings (after `COMMAND_CONFIG_DIR`/`COMMAND_DEFAULT_TIMEOUT`), add:

```python
    # Control_node directory where run-ansible.sh tees per-run logs.
    COMMAND_LOG_DIR: str = "/var/log/ansible-runs"
    # Soft cap → CommandTraceResponse.size_warning (banner, keep polling).
    COMMAND_LOG_SOFT_CAP_BYTES: int = 5 * 1024 * 1024
    # Hard cap → CommandTraceResponse.too_large (viewer stops polling).
    COMMAND_LOG_HARD_CAP_BYTES: int = 10 * 1024 * 1024
    # For `logged` commands: on failure, keep only the last N lines of output in
    # Redis as an error summary (full log lives on the control_node / /view).
    # 0 = store nothing even on failure.
    COMMAND_LOG_FAILURE_TAIL_LINES: int = 50
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_log_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/unit/test_command_log_settings.py
git commit -m "feat(config): add COMMAND_LOG_DIR and command log size caps"
```

---

### Task 3: Inject run_id / run_log_path for logged commands

**Files:**
- Modify: `app/services/command_service.py`
- Test: `tests/unit/test_command_run_id_injection.py`

**Interfaces:**
- Consumes: `ExecutionContext` (has `cmd_config`, `raw_request`, `command_name`), `CommandWhitelistConfig.logged`, `settings.COMMAND_LOG_DIR`.
- Produces:
  - `CommandService._build_pipeline(context)` resolves a `{run_id}` placeholder from `context.run_id` when present.
  - New field `ExecutionContext.run_id: Optional[str] = None` and `ExecutionContext.run_log_path: Optional[str] = None`.
  - `CommandService._compute_log_path(command_id) -> str` returns `f"{settings.COMMAND_LOG_DIR}/{command_id}.log"`.

> **Note on `command_id` timing:** today `command_id` is generated *inside* `_handle_async_execution`. For `logged` commands we must know it *before* `_build_pipeline` so `{run_id}` resolves. This task moves `command_id` generation up into `execute_command` and threads it through. Reuse `command_id` as `run_id` (1:1).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_run_id_injection.py`:

```python
import pytest
from app.domain.command import (
    CommandExecutionRequest, CommandWhitelistConfig, PipelineStep,
    SSHConnectionConfig, ExecutionContext, HostType,
)
from app.repositories.host_resolver import ResolvedHost
from app.services.command_service import CommandService


def _ctx(cmd_config, run_id=None):
    req = CommandExecutionRequest(
        command_name="run_ansible_ping_all", host="localhost",
        host_type=HostType.IP, port=2224, username="root",
        ssh_config="control_node",
        arguments={"inventory": "taipei/multinode.ini"},
    )
    ctx = ExecutionContext(
        username="admin", request_id="r1", command_name="run_ansible_ping_all",
        raw_request=req, cmd_config=cmd_config,
        ssh_config=SSHConnectionConfig(auth_method="key", key_base64="x"),
        resolved_host=ResolvedHost(ip="1.2.3.4"),
    )
    ctx.run_id = run_id
    return ctx


def _svc():
    return CommandService(repo=None, inventory_repo=None)


def test_logged_command_resolves_run_id_placeholder():
    cfg = CommandWhitelistConfig(
        command_name="run_ansible_ping_all", logged=True,
        pipeline=[PipelineStep(command=[
            "/x/run-ansible.sh", "--inventory", "{inventory}",
            "--log-dir", "/var/log/ansible-runs", "--run-id", "{run_id}",
        ])],
        arguments=[],
    )
    ctx = _ctx(cfg, run_id="abc-123")
    # inventory is a user arg; supply it on the request already (done in _ctx)
    pipeline = _svc()._build_pipeline(ctx)
    flat = pipeline[0]
    assert "{run_id}" not in flat
    assert "abc-123" in flat
    assert flat[flat.index("--run-id") + 1] == "abc-123"


def test_compute_log_path():
    path = _svc()._compute_log_path("abc-123")
    assert path == "/var/log/ansible-runs/abc-123.log"
```

> If `ResolvedHost` requires more fields, inspect `app/repositories/host_resolver.py` and supply them. Keep the test minimal.

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_run_id_injection.py -v`
Expected: FAIL — `_compute_log_path` missing and `{run_id}` not resolved (`AssertionError`).

- [ ] **Step 3: Write minimal implementation**

In `app/domain/command.py`, add two fields to `ExecutionContext`:

```python
@dataclass
class ExecutionContext:
    # ... existing fields ...
    conn: Optional[asyncssh.SSHClientConnection] = None
    pipeline_cmds: List[List[str]] = field(default_factory=list)
    run_id: Optional[str] = None
    run_log_path: Optional[str] = None
```

In `app/services/command_service.py`, add the path helper and teach `_resolve_command_part` about `{run_id}`:

```python
    def _compute_log_path(self, command_id: str) -> str:
        """Control_node path where run-ansible.sh tees this run's log."""
        return f"{settings.COMMAND_LOG_DIR}/{command_id}.log"
```

Update `_resolve_command_part` to also resolve the server-injected `{run_id}` token (it is NOT a user argument):

```python
    def _resolve_command_part(self, part: str, arguments: Dict[str, Any], arg_defs: list, run_id: Optional[str] = None) -> str:
        """Replace {placeholder} tokens in a single command part.

        User-argument placeholders come from ``arguments``/``arg_defs``.
        ``{run_id}`` is server-injected (never a user argument) and resolved
        from ``run_id`` when provided.
        """
        for arg in arg_defs:
            placeholder = f"{{{arg.name}}}"
            if placeholder in part:
                part = part.replace(placeholder, str(arguments[arg.name]))
        if run_id is not None and "{run_id}" in part:
            part = part.replace("{run_id}", run_id)
        return part
```

Update `_build_pipeline` to pass `context.run_id` through:

```python
    def _build_pipeline(self, context: ExecutionContext) -> List[List[str]]:
        return [
            [
                self._resolve_command_part(
                    part,
                    context.raw_request.arguments,
                    context.cmd_config.arguments,
                    run_id=context.run_id,
                )
                for part in step.command
            ]
            for step in context.cmd_config.pipeline
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_run_id_injection.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/domain/command.py app/services/command_service.py tests/unit/test_command_run_id_injection.py
git commit -m "feat(command): resolve server-injected {run_id} placeholder and compute log path"
```

---

### Task 4: Wire run_id generation + store run_log_path on CommandState

**Files:**
- Modify: `app/services/command_service.py` (`execute_command`, `_handle_async_execution`)
- Test: `tests/unit/test_command_run_id_injection.py` (add cases)

**Interfaces:**
- Consumes: Task 3 helpers, `CommandState.run_log_path` (Task 1).
- Produces: `execute_command` generates `command_id` (when `logged`) before `_build_pipeline`; `_handle_async_execution` accepts a pre-generated id and persists `run_log_path`.

> Today `_handle_async_execution` does `command_id = str(uuid.uuid4())` internally. Change it to accept an optional pre-generated id so `execute_command` can generate it early for `logged` commands. Non-logged commands keep generating inside (id only needed at registration time).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_command_run_id_injection.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.domain.command import CommandState, CommandStatus


def test_handle_async_persists_run_log_path(monkeypatch):
    cfg = CommandWhitelistConfig(
        command_name="run_ansible_ping_all", logged=True, killable=True,
        pipeline=[PipelineStep(command=["/x/run-ansible.sh", "--run-id", "{run_id}"])],
    )
    ctx = _ctx(cfg, run_id="fixed-id")
    ctx.run_log_path = "/var/log/ansible-runs/fixed-id.log"
    ctx.conn = MagicMock()
    ctx.pipeline_cmds = [["/x/run-ansible.sh", "--run-id", "fixed-id"]]

    repo = MagicMock()
    saved = {}

    async def fake_save(state, ttl):
        saved["state"] = state
    repo.save = AsyncMock(side_effect=fake_save)
    repo.update = AsyncMock()

    svc = CommandService(repo=repo, inventory_repo=None)

    # Stop the background task from actually running SSH.
    monkeypatch.setattr(svc, "_execute_pipeline", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(svc, "_collect_output", AsyncMock(return_value=(0, "ok")))
    monkeypatch.setattr(svc, "_store_result", AsyncMock())

    resp = asyncio.get_event_loop().run_until_complete(
        svc._handle_async_execution(ctx, command_id="fixed-id")
    )
    assert resp.command_id == "fixed-id"
    assert saved["state"].run_log_path == "/var/log/ansible-runs/fixed-id.log"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_run_id_injection.py::test_handle_async_persists_run_log_path -v`
Expected: FAIL — `_handle_async_execution()` got an unexpected keyword `command_id` (or `run_log_path` not on saved state).

- [ ] **Step 3: Write minimal implementation**

In `app/services/command_service.py`, change `_handle_async_execution` signature to accept an optional id, and persist `run_log_path`:

```python
    async def _handle_async_execution(self, context: ExecutionContext, command_id: Optional[str] = None) -> CommandExecutionResponse:
        command_id = command_id or str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        # ... existing entry/_local_running_commands setup unchanged ...
```

In the `CommandState(...)` construction inside that method, add:

```python
        state = CommandState(
            command_id=command_id,
            status=CommandStatus.RUNNING,
            # ... existing fields ...
            exec_command=cmd_str_preview,
            run_log_path=context.run_log_path,
        )
```

In `execute_command`, generate the id early when the command is `logged`, and thread it through:

```python
    async def execute_command(self, username, request_id, req):
        self._check_capacity(username, request_id)
        context = await self._prepare_execution(username, request_id, req)

        command_id = None
        if context.cmd_config.logged:
            command_id = str(uuid.uuid4())
            context.run_id = command_id
            context.run_log_path = self._compute_log_path(command_id)

        context.pipeline_cmds = self._build_pipeline(context)

        conn = await self._connect(context, req)
        context.conn = conn

        if context.cmd_config.disconnects_ssh:
            return await self._handle_fire_and_forget(context)

        return await self._handle_async_execution(context, command_id=command_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_run_id_injection.py -v`
Expected: PASS (all tests in file).

- [ ] **Step 5: Run the full command service unit suite to check no regression**

Run: `APP_ENV=test uv run pytest tests/unit -k command -v`
Expected: PASS (existing command tests still green).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_service.py tests/unit/test_command_run_id_injection.py
git commit -m "feat(command): generate run_id early and persist run_log_path on state"
```

---

### Task 5: `get_command_trace` — tail the remote log over SSH

**Files:**
- Modify: `app/services/command_service.py`
- Test: `tests/unit/test_command_trace.py`

**Interfaces:**
- Consumes: `self.repo.get(command_id) -> CommandState`, `_load_ssh_config`, `create_authenticator`, `LogRenderer`, settings caps.
- Produces:
  - `async CommandService.get_command_trace(command_id: str, byte_offset: int = 0, line_num: int = 1) -> CommandTraceResponse`
  - `async CommandService._read_remote_log(state, byte_offset) -> tuple[int, str]` returning `(total_size, new_text)`. Raises `UpstreamUnavailableException`/`UpstreamTimeoutException` on SSH failure (reuse existing mapping style).

> `_read_remote_log` SSHes to `state.resolved_ip:state.port` and runs two commands as discrete args (no shell metachars in the path — it's server-generated, but still pass as args):
> `stat -c %s <path>` for size, and `tail -c +<offset+1> <path>` for the new bytes. If `stat` fails (file not created yet), treat size as 0 and text as "".

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_trace.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.domain.command import CommandState, CommandStatus
from app.services.command_service import CommandService
from app.core.exceptions import NotFoundException
from app.core.config import get_settings


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/ansible-runs/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _svc_with_state(state):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=state)
    return CommandService(repo=repo, inventory_repo=None)


async def test_trace_no_log_path_returns_empty_with_status():
    svc = _svc_with_state(_state(run_log_path=None, status=CommandStatus.SUCCESS))
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.lines == []
    assert resp.status == "success"
    assert resp.total_size == 0
    assert resp.next_byte_offset == 0


async def test_trace_unknown_command_raises_notfound():
    repo = MagicMock()
    from app.core.exceptions import CommandExecutionException
    repo.get = AsyncMock(side_effect=CommandExecutionException("nope"))
    svc = CommandService(repo=repo, inventory_repo=None)
    with pytest.raises(NotFoundException):
        await svc.get_command_trace("missing")


async def test_trace_happy_path_renders_new_lines(monkeypatch):
    svc = _svc_with_state(_state())
    monkeypatch.setattr(
        svc, "_read_remote_log",
        AsyncMock(return_value=(12, "line one\nline two\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.total_size == 12
    assert resp.next_byte_offset == 12
    assert [l.num for l in resp.lines] == [1, 2]
    assert resp.next_line_num == 3


async def test_trace_hard_cap_stops_serving_lines(monkeypatch):
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    big = get_settings().COMMAND_LOG_HARD_CAP_BYTES + 1
    monkeypatch.setattr(
        svc, "_read_remote_log", AsyncMock(return_value=(big, "x\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.too_large is True
    assert resp.lines == []


async def test_trace_soft_cap_sets_warning_but_serves(monkeypatch):
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    mid = get_settings().COMMAND_LOG_SOFT_CAP_BYTES + 1
    monkeypatch.setattr(
        svc, "_read_remote_log", AsyncMock(return_value=(mid, "hello\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.size_warning is True
    assert resp.too_large is False
    assert len(resp.lines) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_trace.py -v`
Expected: FAIL — `AttributeError: 'CommandService' object has no attribute 'get_command_trace'`.

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `app/services/command_service.py` (if not present):

```python
from app.domain.command import CommandLogLine, CommandTraceResponse
from app.core.log_renderer import LogRenderer
```

Add the two methods to `CommandService`:

```python
    async def _read_remote_log(self, state: "CommandState", byte_offset: int) -> tuple[int, str]:
        """SSH to the control_node and read the run log tail.

        Returns (total_size, new_text). If the file does not exist yet
        (run just started), returns (0, ""). Path is server-generated and
        passed as a discrete argument.
        """
        ssh_config = self._load_ssh_config(state.ssh_config)
        authenticator = create_authenticator(ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        path = state.run_log_path
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=state.resolved_ip, port=state.port,
                    username=state.username, **conn_kwargs,
                ),
                timeout=settings.SSH_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise UpstreamTimeoutException(
                f"SSH connect to read log timed out for {state.command_id}.",
                detail={"command_id": state.command_id},
            ) from exc
        except (OSError, asyncssh.Error) as exc:
            raise UpstreamUnavailableException(
                f"SSH connect to read log failed for {state.command_id}: {exc}",
                detail={"command_id": state.command_id},
            ) from exc
        try:
            size_res = await conn.run("stat", "-c", "%s", path, check=False)
            if size_res.exit_status != 0:
                return 0, ""  # file not created yet
            total_size = int(str(size_res.stdout).strip() or "0")
            tail_res = await conn.run(
                "tail", "-c", f"+{byte_offset + 1}", path, check=False,
            )
            new_text = str(tail_res.stdout) if tail_res.stdout else ""
            return total_size, new_text
        finally:
            conn.close()

    async def get_command_trace(self, command_id: str, byte_offset: int = 0, line_num: int = 1) -> CommandTraceResponse:
        """Incremental tail of a logged command's run log for the UI viewer."""
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException as exc:
            raise NotFoundException(
                f"Command {command_id} not found.",
                detail={"command_id": command_id},
            ) from exc

        status = state.status.value if hasattr(state.status, "value") else str(state.status)

        if not state.run_log_path:
            return CommandTraceResponse(
                command_id=command_id, status=status,
                next_byte_offset=byte_offset, next_line_num=line_num, lines=[],
            )

        total_size, new_text = await self._read_remote_log(state, byte_offset)

        if total_size > settings.COMMAND_LOG_HARD_CAP_BYTES:
            return CommandTraceResponse(
                command_id=command_id, status=status,
                next_byte_offset=byte_offset, next_line_num=line_num,
                lines=[], total_size=total_size, too_large=True,
            )

        size_warning = total_size > settings.COMMAND_LOG_SOFT_CAP_BYTES

        # Hold back a trailing partial line so we never render half a line.
        next_byte_offset = total_size
        if new_text and not new_text.endswith("\n"):
            last_nl = new_text.rfind("\n")
            if last_nl == -1:
                return CommandTraceResponse(
                    command_id=command_id, status=status,
                    next_byte_offset=byte_offset, next_line_num=line_num,
                    lines=[], total_size=total_size, size_warning=size_warning,
                )
            held_back = len(new_text) - (last_nl + 1)
            new_text = new_text[: last_nl + 1]
            next_byte_offset = total_size - held_back

        if not new_text:
            return CommandTraceResponse(
                command_id=command_id, status=status,
                next_byte_offset=next_byte_offset, next_line_num=line_num,
                lines=[], total_size=total_size, size_warning=size_warning,
            )

        rendered = LogRenderer().render(0, new_text, start_line_num=line_num)
        lines = [CommandLogLine(num=l.num, content_html=l.content_html) for l in rendered]

        return CommandTraceResponse(
            command_id=command_id, status=status,
            next_byte_offset=next_byte_offset,
            next_line_num=line_num + len(lines),
            lines=lines, total_size=total_size, size_warning=size_warning,
        )
```

> Note: `byte_offset` count semantics — `tail -c +N` is **1-indexed** from the start, so byte offset `0` → `+1` returns the whole file; an offset of `total_size` → `+(size+1)` returns empty. This matches `next_byte_offset = total_size`.

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_trace.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/command_service.py tests/unit/test_command_trace.py
git commit -m "feat(command): add get_command_trace to tail control_node run log over SSH"
```

---

### Task 5b: Output policy for logged commands (trim Redis output)

> Numbered 5b (inserted after the plan's first draft) so the later task numbers
> stay stable. It depends on Task 1 (`logged` flag), Task 2
> (`COMMAND_LOG_FAILURE_TAIL_LINES`), and Task 4 (early `command_id` +
> `_handle_async_execution(context, command_id=...)`). Independent of the
> viewer endpoints (Tasks 6–7), so it may run any time after Task 4.

**Files:**
- Modify: `app/services/command_service.py`
- Test: `tests/unit/test_command_output_policy.py`

**Interfaces:**
- Consumes: `cmd_config.logged` (Task 1), `settings.COMMAND_LOG_FAILURE_TAIL_LINES` (Task 2), the existing `_collect_output` return `(returncode, output)` and `CommandExecutionResponse.success/.failed`.
- Produces: `CommandService._apply_output_policy(logged: bool, success: bool, output: str) -> Optional[str]` — the value to persist as `CommandState.output`.

**Policy (from spec "Output policy for logged commands"):**
- Non-`logged` command → return `output` unchanged (current behaviour).
- `logged` + success → return `None` (full log is on the control_node / `/view`).
- `logged` + failure → return only the last `COMMAND_LOG_FAILURE_TAIL_LINES`
  lines of `output` (joined with `\n`); if that setting is `0`, return `None`.

> Success/failure determination is unchanged — it comes from the SSH process
> exit status (`returncode == 0`) computed in `_handle_async_execution`. This
> task only changes *what output we persist*, never the status.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_output_policy.py`:

```python
from app.services.command_service import CommandService
from app.core.config import get_settings


def _svc():
    return CommandService(repo=None, inventory_repo=None)


def test_non_logged_keeps_full_output():
    out = "\n".join(f"line{i}" for i in range(200))
    assert _svc()._apply_output_policy(logged=False, success=True, output=out) == out
    assert _svc()._apply_output_policy(logged=False, success=False, output=out) == out


def test_logged_success_drops_output():
    out = "\n".join(f"line{i}" for i in range(200))
    assert _svc()._apply_output_policy(logged=True, success=True, output=out) is None


def test_logged_failure_keeps_last_50_lines():
    get_settings.cache_clear()
    n = get_settings().COMMAND_LOG_FAILURE_TAIL_LINES  # 50
    out = "\n".join(f"line{i}" for i in range(200))
    result = _svc()._apply_output_policy(logged=True, success=False, output=out)
    kept = result.split("\n")
    assert len(kept) == n
    assert kept[0] == f"line{200 - n}"
    assert kept[-1] == "line199"


def test_logged_failure_shorter_than_tail_kept_whole():
    out = "only\ntwo lines"
    result = _svc()._apply_output_policy(logged=True, success=False, output=out)
    assert result == out


def test_logged_failure_with_empty_output():
    assert _svc()._apply_output_policy(logged=True, success=False, output="") in (None, "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_output_policy.py -v`
Expected: FAIL — `AttributeError: 'CommandService' object has no attribute '_apply_output_policy'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/command_service.py`, add the helper:

```python
    def _apply_output_policy(self, logged: bool, success: bool, output: str) -> Optional[str]:
        """Decide what output to persist on CommandState for a finished command.

        Non-logged commands keep their full output (legacy behaviour). Logged
        commands rely on the control_node file + /view for the full log, so we
        persist nothing on success and only a short failure tail on failure.
        """
        if not logged:
            return output
        if success:
            return None
        tail_lines = settings.COMMAND_LOG_FAILURE_TAIL_LINES
        if tail_lines <= 0 or not output:
            return None if not output else None if tail_lines <= 0 else output
        lines = output.split("\n")
        return "\n".join(lines[-tail_lines:])
```

> Simplify the empty/zero branch to match the test (`""` may return `None` or
> `""`; both are accepted). A clearer equivalent:
> ```python
>         if success:
>             return None
>         tail_lines = settings.COMMAND_LOG_FAILURE_TAIL_LINES
>         if tail_lines <= 0:
>             return None
>         if not output:
>             return None
>         return "\n".join(output.split("\n")[-tail_lines:])
> ```
> Use this clearer version.

Then apply it in `_handle_async_execution`, where the result is built after
`_collect_output`. Replace the result-construction block:

```python
                returncode, output = await self._collect_output(final_process)

                logger.info(
                    f"Command '{context.command_name}' finished. Exit Status: {returncode}",
                    extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
                )

                success = returncode == 0
                stored_output = self._apply_output_policy(
                    context.cmd_config.logged, success, output,
                )
                if success:
                    res = CommandExecutionResponse.success(command_id=command_id, exit_status=returncode, output=stored_output or "")
                else:
                    res = CommandExecutionResponse.failed(message="", exit_status=returncode, output=stored_output, command_id=command_id)
                await self._store_result(command_id, res)
```

> Only the `success`/`stored_output` lines are new; the surrounding logging and
> `_store_result` call are unchanged. `CommandExecutionResponse.success` expects
> a non-optional `output`, so pass `stored_output or ""`.

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_output_policy.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the command unit suite for regressions**

Run: `APP_ENV=test uv run pytest tests/unit -k command -v`
Expected: PASS (existing command tests unaffected — non-logged path unchanged).

- [ ] **Step 6: Commit**

```bash
git add app/services/command_service.py tests/unit/test_command_output_policy.py
git commit -m "feat(command): trim Redis output for logged commands (none on success, tail on failure)"
```

---

### Task 6: Parameterise the HTML viewer template

**Files:**
- Modify: `app/core/log_viewer_template.py`
- Modify: `app/api/v1/deploy.py` (`view_job` — keep behaviour identical)
- Test: `tests/unit/test_log_viewer_template.py`

**Interfaces:**
- Produces: `LOG_VIEWER_HTML` exposes format slots `{title}`, `{heading}`, `{trace_url}`, `{terminal_statuses_json}`, `{meta_html}`. The fetch URL and terminal-status list are no longer hardcoded to GitLab.
- Consumes (deploy): `view_job` fills the slots with the existing GitLab values so its output/behaviour is unchanged.

> The current template hardcodes the deploy trace URL on line ~406, a GitLab `TERMINAL_STATUSES` list (line ~289) that lacks `killed`, and `projectId`/`jobId` meta. Replace these with slots. The command viewer (Task 7) supplies `killed` in its terminal statuses and an empty/relevant `meta_html`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_log_viewer_template.py`:

```python
from app.core.log_viewer_template import LOG_VIEWER_HTML


def test_template_has_parameterised_slots():
    # The deploy-specific hardcoded URL must be gone; slots must exist.
    assert "{trace_url}" in LOG_VIEWER_HTML
    assert "{terminal_statuses_json}" in LOG_VIEWER_HTML
    assert "{title}" in LOG_VIEWER_HTML
    assert "{heading}" in LOG_VIEWER_HTML
    assert "/api/v1/deploy/jobs/{job_id}/trace/ui" not in LOG_VIEWER_HTML


def test_template_formats_for_command_viewer():
    html = LOG_VIEWER_HTML.format(
        title="Command Log | c1",
        heading="Command: c1",
        trace_url="/api/v1/command/execution/c1/trace/ui",
        terminal_statuses_json="['success','failed','killed']",
        meta_html="<div>Command ID c1</div>",
    )
    assert "/api/v1/command/execution/c1/trace/ui" in html
    assert "killed" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_log_viewer_template.py -v`
Expected: FAIL — slots absent / old URL still present.

- [ ] **Step 3: Write minimal implementation**

Edit `app/core/log_viewer_template.py`:

1. Title (line ~14): `<title>{title}</title>`
2. Heading (line ~248): `<h1>{heading}</h1>`
3. Terminal statuses (line ~289): replace the hardcoded array with the slot:
   ```javascript
   const TERMINAL_STATUSES = {terminal_statuses_json};
   ```
4. Fetch URL (line ~406): replace the hardcoded deploy URL with:
   ```javascript
   const res = await fetch(`{trace_url}?byte_offset=${{currentByteOffset}}&line_num=${{currentLineNum}}&t=${{Date.now()}}`, {{ cache: 'no-store' }});
   ```
   (Drop `&project_id=...` from the base; deploy passes it inside `{trace_url}` instead — see deploy update below.)
5. Replace the GitLab-specific meta rows in the two error panels (lines ~374-375 and ~384-385, `Project ID`/`Job ID`) and the `gitlabUrl`/`projectId`/`jobId`/`jobLink` consts (lines ~291-296) with a single `{meta_html}` slot rendered where those rows were, and remove the now-unused consts. Keep the panels rendering `{meta_html}` for both error branches.

Then update `app/api/v1/deploy.py` `view_job` to fill the slots, preserving current output:

```python
    trace_url = f"/api/v1/deploy/jobs/{job_id}/trace/ui?project_id={target_project_id}"
    meta_html = (
        f'<div><span class="label">Project ID</span><code>{target_project_id}</code></div>'
        f'<div><span class="label">Job ID</span><code>{job_id}</code></div>'
        f'<div><a href="{job_web_url}" target="_blank">Open in GitLab</a></div>'
    )
    return LOG_VIEWER_HTML.format(
        title=f"Job Log Viewer | {job_id}",
        heading=f"Job: {job_id}",
        trace_url=trace_url,
        terminal_statuses_json="['success','failed','canceled','skipped','manual']",
        meta_html=meta_html,
    )
```

> Note: because `trace_url` already contains `?project_id=...`, the JS appends `&byte_offset=...`. Change the fetch template's leading `?` to handle this: use `{trace_url}` already containing a `?` and append params with `&`. Adjust the deploy `trace_url` to end without a query when none is needed, OR keep the JS using a `sep` computed as `{trace_url}.includes('?') ? '&' : '?'`. Implement the `sep` approach to keep both viewers correct:
> ```javascript
> const sep = `{trace_url}`.includes('?') ? '&' : '?';
> const res = await fetch(`{trace_url}${{sep}}byte_offset=${{currentByteOffset}}&line_num=${{currentLineNum}}&t=${{Date.now()}}`, {{ cache: 'no-store' }});
> ```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_log_viewer_template.py -v`
Expected: PASS.

- [ ] **Step 5: Verify deploy viewer still renders**

Run: `APP_ENV=test uv run pytest tests/integration -k view -v`
Expected: PASS (existing deploy view test, if any). If none exists, manually assert the format call doesn't raise:
Run: `APP_ENV=test uv run python -c "from app.core.log_viewer_template import LOG_VIEWER_HTML; print('ok' if '{' not in LOG_VIEWER_HTML.format(title='t',heading='h',trace_url='/u',terminal_statuses_json='[]',meta_html='')[:50] else 'ok')"`
Expected: prints `ok` (no `KeyError`/`IndexError` from stray unescaped braces).

- [ ] **Step 6: Commit**

```bash
git add app/core/log_viewer_template.py app/api/v1/deploy.py tests/unit/test_log_viewer_template.py
git commit -m "refactor(viewer): parameterise log viewer template; deploy passes its slots"
```

---

### Task 7: Command API endpoints — trace/ui + view

**Files:**
- Modify: `app/api/v1/command.py`
- Test: `tests/integration/test_command_trace_api.py`

**Interfaces:**
- Consumes: `CommandService.get_command_trace(...)` (Task 5), `LOG_VIEWER_HTML` slots (Task 6), `get_command_service`, `get_current_user(["command_api"])`.
- Produces:
  - `GET /api/v1/command/execution/{command_id}/trace/ui?byte_offset=0&line_num=1 → ApiResponse[CommandTraceResponse]` (scope `command_api`).
  - `GET /api/v1/command/execution/{command_id}/view → HTMLResponse`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_command_trace_api.py`:

```python
import pytest


def _token(client):
    r = client.post("/token", data={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_trace_ui_requires_scope(client):
    r = client.get("/api/v1/command/execution/whatever/trace/ui")
    assert r.status_code in (401, 403)


def test_trace_ui_unknown_command_404(client):
    tok = _token(client)
    r = client.get(
        "/api/v1/command/execution/does-not-exist/trace/ui",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 404
    body = r.json()
    assert "request_id" in body or "detail" in body  # structured error envelope


def test_view_returns_html_with_trace_url(client):
    r = client.get("/api/v1/command/execution/c1/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/api/v1/command/execution/c1/trace/ui" in r.text
```

> Check the existing `client` fixture name in `tests/conftest.py`; if it's `test_client`, adjust. The admin token uses the fixture user in `tests/fixtures/users.json` — confirm `admin`/`password` works there (it's used in `ansible.http`). If the fixture user differs, use that one.

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/integration/test_command_trace_api.py -v`
Expected: FAIL — 404 routes not registered (trace/ui & view return 404 for the wrong reason, or view returns 404).

- [ ] **Step 3: Write minimal implementation**

In `app/api/v1/command.py`, add imports:

```python
from fastapi import Query
from fastapi.responses import HTMLResponse
from app.domain.command import CommandTraceResponse
from app.core.log_viewer_template import LOG_VIEWER_HTML
```

Add the trace endpoint (scoped) and the view endpoint (unauthed HTML shell, mirroring deploy `view_job`):

```python
@router.get(
    "/execution/{command_id}/trace/ui",
    response_model=ApiResponse[CommandTraceResponse],
    summary="Get formatted command logs for UI",
    description="Incremental tail of the control_node run log; poll with byte_offset.",
)
async def get_command_trace_ui(
    command_id: str,
    request: Request,
    byte_offset: int = Query(0, ge=0),
    line_num: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandTraceResponse]:
    data = await svc.get_command_trace(command_id, byte_offset, line_num)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}/view",
    response_class=HTMLResponse,
    summary="View command logs in UI",
    description="Auto-refreshing log viewer for a long-running command.",
)
async def view_command(command_id: str):
    trace_url = f"/api/v1/command/execution/{command_id}/trace/ui"
    meta_html = f'<div><span class="label">Command ID</span><code>{command_id}</code></div>'
    return LOG_VIEWER_HTML.format(
        title=f"Command Log Viewer | {command_id}",
        heading=f"Command: {command_id}",
        trace_url=trace_url,
        terminal_statuses_json="['success','failed','killed']",
        meta_html=meta_html,
    )
```

> Route ordering: ensure these are added to the existing `command` router (prefix `/command`) so the full paths are `/api/v1/command/execution/{command_id}/trace/ui` and `.../view`. The existing `GET /{command_name}/info` route uses a path param at the same level — FastAPI matches the more specific `/execution/...` literal segment fine, but place these BEFORE any catch-all `/{command_name}` route if one exists to avoid shadowing. Verify against the current router order in `command.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/integration/test_command_trace_api.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/command.py tests/integration/test_command_trace_api.py
git commit -m "feat(command): add trace/ui and view endpoints for command log viewer"
```

---

### Task 8: `run-ansible.sh` — per-run log file + self-cleaning (tests)

**Status:** The script changes are **already implemented and manually verified**
(per-run `--run-id` log file, `--log-retention-days` default 3 self-cleaning,
`DRYRUN` hook). This task adds the regression tests and commits both together.

**Files:**
- Already modified: `ansible/run-ansible.sh`
- Test (create): `tests/integration/test_run_ansible_script.py` (subprocess-based; no docker needed — `DRYRUN=1` exits before the inventory clone / `docker run`)

**Interfaces (as implemented in the script):**
- `--run-id <id>` → tees to `<log-dir>/<id>.log`; rejects `<id>` not matching `^[A-Za-z0-9_-]+$` (exit 2). Unset → `<log-dir>/run.log` (legacy behaviour).
- `--log-retention-days <n>` (default **3**) → at the **start** of each run, prunes `<log-dir>/*.log` older than `n` days via `find … -mtime +<n> -delete`, guarded by `[[ -n "$LOG_DIR" && -d "$LOG_DIR" ]]`. `0` disables cleanup. Non-integer → exit 2.
- `DRYRUN=1` env → prints `DRYRUN log file: <path>` and exits 0 after arg validation + the self-cleaning prune, before any docker/git work.

> The script is the source of truth; if a test below disagrees with the script,
> re-read `ansible/run-ansible.sh` and fix the test to match the verified
> behaviour (do not regress the script).

- [ ] **Step 1: Write the tests**

Create `tests/integration/test_run_ansible_script.py`:

```python
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
```

- [ ] **Step 2: Run the tests (script already implements the behaviour → they pass)**

Run: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: PASS (5 tests). The script change predates these tests, so this is a
characterization/regression suite — it should be green immediately. If any test
fails, the script and test disagree: re-read the script and fix the test to the
verified behaviour.

- [ ] **Step 3: Confirm bash syntax is intact**

Run: `bash -n ansible/run-ansible.sh && echo OK`
Expected: prints `OK`.

- [ ] **Step 4: Commit**

```bash
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(ansible): per-run log file via --run-id + self-cleaning (default 3 days)"
```

---

### Task 9: Whitelist config — enable logging for ansible commands

**Files:**
- Modify: `data/allow-commands-admin.json`
- Modify: `data/allow-commands-test_admin.json`
- Test: `tests/unit/test_whitelist_logged.py`

**Interfaces:**
- Consumes: `logged` flag (Task 1), `{run_id}` placeholder resolution (Task 3), `--log-dir`/`--run-id` args.
- Produces: every `run_ansible_*` entry has `"logged": true` and its pipeline command includes `--log-dir <dir>` and `--run-id {run_id}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_whitelist_logged.py`:

```python
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"


def _load(name):
    return json.loads((DATA / name).read_text())


def test_admin_ansible_commands_are_logged():
    cfg = _load("allow-commands-admin.json")
    ansible = [c for c in cfg["allow_commands"] if c["command_name"].startswith("run_ansible")]
    assert ansible, "expected run_ansible_* commands"
    for c in ansible:
        assert c.get("logged") is True, c["command_name"]
        flat = [tok for step in c["pipeline"] for tok in step["command"]]
        assert "--run-id" in flat and "{run_id}" in flat, c["command_name"]
        assert "--log-dir" in flat, c["command_name"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_whitelist_logged.py -v`
Expected: FAIL — `logged` absent / `--run-id` not in pipeline.

- [ ] **Step 3: Write minimal implementation**

In `data/allow-commands-admin.json`, for each `run_ansible_*` entry:
- Add `"logged": true,` (next to `"killable": true,`).
- Append to that entry's first pipeline `command` array (after the existing args, before/around `--no-pull`):
  ```json
  "--log-dir", "/var/log/ansible-runs",
  "--run-id", "{run_id}",
  ```

> Use the configured dir. For local dev where logs aren't under `/var/log`, the `--log-dir` value here should match `COMMAND_LOG_DIR`. If local runs need a different path, override `COMMAND_LOG_DIR` in `.env.dev` AND the `--log-dir` literal here together (they must agree — the service computes the path from `COMMAND_LOG_DIR`, the script writes to `--log-dir`). Document this coupling in a comment-free way by keeping both equal to `/var/log/ansible-runs` by default.

Apply the same edits to `data/allow-commands-test_admin.json` for its `run_ansible_*` entries (if present; if that file has none, the test should only assert on `admin` — adjust the test to the files that actually contain ansible commands).

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_whitelist_logged.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data/allow-commands-admin.json data/allow-commands-test_admin.json tests/unit/test_whitelist_logged.py
git commit -m "feat(config): enable run-log capture for ansible whitelist commands"
```

---

### Task 10: Full suite + manual smoke verification

**Files:**
- No code changes (verification only). Optionally update `rest_client/ansible.http`.

- [ ] **Step 1: Run the complete test suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: PASS (all tasks' tests green, no regressions).

- [ ] **Step 2: Coverage on the touched modules**

Run: `APP_ENV=test uv run pytest tests/ --cov=app.services.command_service --cov=app.api.v1.command --cov-report=term-missing -q`
Expected: command_service trace paths and the two new endpoints are covered.

- [ ] **Step 3: Add a viewer request to `ansible.http` (optional but recommended)**

Append to `rest_client/ansible.http`:

```
### Open the live log viewer for the last command in a browser
# (paste the command_id; open http://localhost:8001/api/v1/command/execution/<id>/view)
GET {{base_url}}/api/v1/command/execution/{{my_command_id}}/trace/ui?byte_offset=0&line_num=1
Authorization: Bearer {{token}}
```

- [ ] **Step 4: Manual smoke (requires control_node + redis up)**

```bash
make redis-up
make dev   # in another shell
# Then run the run_ansible_ping_all request from ansible.http, grab command_id,
# open http://localhost:8001/api/v1/command/execution/<command_id>/view
```
Expected: viewer page loads, status badge shows RUNNING then SUCCESS, log lines stream in, polling stops at terminal status.

> If local control_node writes logs somewhere other than `/var/log/ansible-runs`, set `COMMAND_LOG_DIR` in `.env.dev` and the `--log-dir` literal in the whitelist to the SAME path before this smoke test.

- [ ] **Step 5: Commit any verification artifacts**

```bash
git add rest_client/ansible.http
git commit -m "docs(ansible): add command log viewer request to rest client"
```

---

## Self-Review Notes

- **Spec coverage:** run-ansible per-run file + self-cleaning (Task 8), CommandState pointer (Task 1/4), get_command_trace offset poll (Task 5), output policy for logged commands (Task 5b), trace/ui + view endpoints (Task 7), reused viewer template (Task 6), logged opt-in flag + whitelist (Task 1/9), size caps + error handling (Task 5), settings incl. `COMMAND_LOG_FAILURE_TAIL_LINES` (Task 2). ✓
- **Output policy:** `/execution/{id}` contract (status/exit_status) unchanged; only the persisted `output` is trimmed for `logged` commands (Task 5b). Non-logged commands keep full output. Success/failure determination still from process exit status. ✓
- **Anti-injection:** `run_log_path`/`run_id` flow as discrete args (Task 3 test asserts the array; Task 5 passes `stat`/`tail` args discretely). ✓
- **Type consistency:** `get_command_trace(command_id, byte_offset, line_num)`, `_read_remote_log(state, byte_offset)->(int,str)`, `_compute_log_path(command_id)->str`, `_apply_output_policy(logged, success, output)->Optional[str]`, `CommandTraceResponse`/`CommandLogLine` field names match across Tasks 1/5/5b/7. Template slots `{title}/{heading}/{trace_url}/{terminal_statuses_json}/{meta_html}` consistent across Tasks 6/7. ✓
- **Coupling flagged:** `COMMAND_LOG_DIR` (service) must equal `--log-dir` (whitelist) — called out in Tasks 9 and 10.
```
