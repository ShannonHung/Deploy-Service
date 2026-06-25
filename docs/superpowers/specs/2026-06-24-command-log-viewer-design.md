# Command Execution Log Viewer — Design

**Date:** 2026-06-24
**Status:** Approved (design), pending spec review
**Scope:** `deploy-service/`

## Problem

The SSH command API (`/api/v1/command/execution/...`) is used to run ansible
playbooks on a control_node. A playbook can run for up to an hour and produce a
very large log. Today the user only learns success/failure by polling
`GET /api/v1/command/execution/{id}`, and the full output is returned **only
after the run finishes** — `_collect_output` calls `process.communicate()`,
which buffers all of stdout/stderr until exit and writes it to Redis in one
shot. During the run the user sees nothing, and a multi-MB log in a single
Redis key is awkward.

We want the same experience as the deploy job view (`/api/v1/deploy/jobs/{job_id}/view`):
an auto-refreshing HTML page that shows the **live, then complete** logs, while
`/execution/{id}` continues to report only success/failure.

## Decisions (locked)

1. **Live preferred, complete log persists.** The viewer tails output in
   near-real-time during the run, and the full log remains viewable after
   completion for later review.
2. **Logs live on the control_node filesystem.** Output is written to a file
   on the control_node (not streamed into Redis). Cleanup is **self-cleaning**:
   `run-ansible.sh` prunes its own log dir at the start of every run, deleting
   `*.log` files older than a retention window (`--log-retention-days`, default
   **3 days**). This replaces a separate cronjob. A MinIO/object-store solution
   is explicitly deferred (YAGNI for now).
3. **Reuse the deploy viewer pattern.** Add a `/view` HTML endpoint and a
   byte-offset trace endpoint for commands, mirroring the deploy job viewer.
4. **`run-ansible.sh` owns the log file (Option A).** deploy-service generates
   a `run_id`, passes it to the script as a discrete argument; the script tees
   its output to `/<log-dir>/<run_id>.log`. deploy-service reads it back by
   offset over SSH. Rationale: the log survives an SSH-channel drop mid-run
   (critical for hour-long runs), and we avoid injecting a shell `| tee` into
   the `setsid … sh -c 'exec "$@"'` anti-injection structure that
   `command_service` relies on.
5. **Offset poll over SSH (Option A).** Each trace poll runs
   `tail -c +<offset>` on the control_node and returns new bytes + new offset.
   Stateless, cross-pod friendly, and `live`/`replay` share one code path.
   No long-lived `tail -f` channel to manage.

## Non-goals

- Object storage / MinIO (deferred).
- Live logs for non-ansible commands. Only commands whose whitelist entry
  opts into file logging get a viewer. Generalising the `tee` to every command
  is out of scope.
- Changing the existing `/execution/{id}` poll contract or the Redis state
  machine (`RUNNING → KILLING → KILLED / SUCCESS / FAILED`).

## Architecture

```
client ──▶ GET /command/execution/{id}/view        (HTML, auto-refresh)
            │
            └─ JS polls ▼
client ──▶ GET /command/execution/{id}/trace/ui?byte_offset=N&line_num=M
                        │
        CommandService.get_command_trace(id, byte_offset, line_num)
                        │  reads run_log_path + ssh_config from Redis CommandState
                        ▼
        SSH to control_node ──▶ tail -c +<N+1> /var/log/ansible-runs/<run_id>.log
                        │  (+ stat for total size)
                        ▼
        format bytes → FormattedLogResponse (lines, next_byte_offset, status…)
```

Execution side (unchanged shape, new field):

```
POST /command/execution
   → _prepare_execution → _build_pipeline (inject run_id arg for logged commands)
   → _connect → _handle_async_execution
        run_id generated, run_log_path stored on CommandState in Redis
        run-ansible.sh tees container stdout/stderr to <log-dir>/<run_id>.log
```

## Components

### 1. `run-ansible.sh` — per-run log file

The script **already** tees to `"$LOG_DIR/run.log"` (line 160) and already has a
`--log-dir` flag. Change:

- Add a `--run-id <id>` flag. When provided, tee target becomes
  `"$LOG_DIR/<run_id>.log"` instead of the fixed `run.log`.
- `--run-id` is validated to match `^[A-Za-z0-9_-]+$` (it's a UUID from
  deploy-service, but defend in depth — it becomes a filename).
- Add a `--log-retention-days <n>` flag (default 3). At the **start** of every
  run (before the inventory clone / docker work, so a long or killed run never
  skips it), prune `<log-dir>/*.log` files older than `n` days:
  `find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime +<n> -delete`,
  guarded by `[[ -n "$LOG_DIR" && -d "$LOG_DIR" ]]` so an empty dir variable can
  never widen the delete scope. Concurrent runs are safe — only files older than
  the window are touched, never the in-flight `<run_id>.log`. This is the
  self-cleaning mechanism that replaces a cronjob.
- Default `LOG_DIR` for the control_node deployment should be a stable
  directory (e.g. `/var/log/ansible-runs`), passed via
  `--log-dir` from the whitelist. The script keeps its current `./logs` default
  for standalone use.
- `set -o pipefail` already ensures a non-zero ansible exit propagates through
  the tee — keep it.

The whitelist pipeline entries for `run_ansible_*` get `--log-dir` and a
`{run_id}` placeholder argument (see §4).

### 2. `CommandState` — store the log pointer

`app/domain/command.py`, `CommandState`:

- Add `run_log_path: Optional[str] = None`. Set at submission time in
  `_handle_async_execution` for commands that have file logging enabled.
- This is the only new persisted field. The fat log itself never enters Redis;
  Redis holds the pointer (path) + the already-present `ssh_config`,
  `resolved_ip`, `port`, `username` needed to SSH back and read it.

### 3. `CommandService.get_command_trace(...)`

New method, mirroring `DeployService.get_formatted_job_trace`:

```
async def get_command_trace(
    self, command_id: str, byte_offset: int, line_num: int
) -> CommandTraceResponse
```

- Load `CommandState` from Redis (404 via `NotFoundException` if absent — same
  as `get_command_execution_result`).
- If `run_log_path` is `None`, return an empty trace with the current `status`
  (command has no file log — e.g. a non-ansible command). The viewer shows
  "no logs for this command".
- Otherwise SSH to `resolved_ip:port` (reuse `_load_ssh_config` +
  `create_authenticator`, the exact pattern `kill_command` already uses for
  cross-pod reconnect), and run two cheap commands:
  - `stat -c %s <path>` → `total_size` (and to detect "file not created yet").
  - `tail -c +<byte_offset+1> <path>` → only the new bytes since last poll.
- Format new bytes into lines and build the response: assign sequential line
  numbers starting at `line_num`, compute `next_byte_offset = byte_offset + len(new_bytes)`
  and `next_line_num`.
- Apply the same soft-cap / hard-cap size guard the deploy viewer has
  (`size_warning` / `too_large`) so a runaway log doesn't hang the page. New
  settings: `COMMAND_LOG_SOFT_CAP_BYTES`, `COMMAND_LOG_HARD_CAP_BYTES`.
- `status` in the response comes from `CommandState.status`. The JS stops
  polling once status is terminal (`success`/`failed`/`killed`) **and** offset
  has caught up to `total_size`.

**Path safety:** `run_log_path` is server-generated (`<log-dir>/<uuid>.log`),
never user input, and is passed to `tail`/`stat` as a discrete `shlex`-quoted
argument via the existing `setsid … sh -c 'exec "$@"'` wrapper — no new
injection surface.

### 4. Whitelist: opt-in file logging + run_id injection

The `run_ansible_*` entries in `data/allow-commands-*.json`:

- Add `--log-dir /var/log/ansible-runs` and `--run-id {run_id}` to the
  pipeline `command` array.
- `{run_id}` is a **service-injected** placeholder, not a user argument. It is
  NOT listed under `arguments` (so it's not user-supplied / regex-validated as
  a request arg). `_build_pipeline` resolves it from the generated id.

Mechanism: extend the whitelist config with an opt-in flag rather than
hard-coding "ansible". Add `logged: bool = False` to `CommandWhitelistConfig`.
When `logged` is true:
- `_handle_async_execution` generates `run_id` (reuse `command_id` — they're
  1:1 and it keeps the filename traceable to the poll id), computes
  `run_log_path`, stores it on `CommandState`, and makes `run_id` available to
  `_build_pipeline`'s placeholder resolution.
- `_resolve_command_part` gains awareness of the `{run_id}` placeholder
  (resolved from context, alongside the existing user-argument placeholders).

This keeps the "only ansible is logged" behaviour as data/config, while leaving
the door open to flag any future long-running command as `logged` without code
changes.

### 5. API endpoints

`app/api/v1/command.py`:

```
GET /api/v1/command/execution/{command_id}/trace/ui
    ?byte_offset=0&line_num=1
    → ApiResponse[CommandTraceResponse]      (scope: command_api)

GET /api/v1/command/execution/{command_id}/view
    → HTMLResponse                            (auto-refreshing viewer)
```

- `/trace/ui` is the JSON polling endpoint; gated by `command_api` scope like
  the rest of command routes.
- `/view` returns the HTML viewer. Mirror `deploy.view_job`: it does not need a
  body, just renders the template pointed at the command's `/trace/ui` URL.

**Auth note for `/view`:** the deploy `/jobs/{id}/view` endpoint is *unauthed*
(the HTML is harmless; the data endpoint it polls carries its own token via the
Swagger flow). Mirror that — `/view` serves only the shell HTML; the
`/trace/ui` endpoint it polls remains `command_api`-scoped. Confirm this
matches the deploy viewer's actual auth posture during implementation and keep
them consistent.

### 6. Response model

`app/domain/command.py` — new model mirroring `FormattedLogResponse` but keyed
by `command_id` and carrying command status:

```python
class CommandLogLine(BaseModel):
    num: int
    content_html: str

class CommandTraceResponse(BaseModel):
    command_id: str
    status: str                 # from CommandState.status
    next_byte_offset: int
    next_line_num: int
    lines: list[CommandLogLine]
    total_size: int = 0
    size_warning: bool = False
    too_large: bool = False
```

Line formatting (ANSI → HTML, timestamps) can reuse whatever helper
`get_formatted_job_trace` uses; if that logic is GitLab-coupled, extract the
pure "bytes → formatted lines" part into a shared helper so both the deploy and
command viewers call it. Prefer extraction over duplication.

### 7. HTML viewer template

`LOG_VIEWER_HTML` is currently hardcoded to the GitLab trace URL,
`project_id`, and a `job_web_url` "open in GitLab" link. Parameterise the
**fetch URL** and the **title/heading** so the same template serves both:

- Replace the hardcoded `/api/v1/deploy/jobs/{job_id}/trace/ui?...` with a
  `{trace_url}` format slot (the command viewer passes the command trace URL;
  the deploy viewer passes its existing URL).
- The "open in GitLab" link is deploy-specific; make it an optional
  `{external_link_html}` slot that the command viewer leaves empty.
- Keep one template file. If parameterising cleanly is awkward, a thin second
  template is acceptable, but one shared template is preferred.

## Data flow (end to end)

1. `POST /command/execution` with `command_name: run_ansible_ping_all`.
2. `_handle_async_execution`: `command_id = uuid`; because the whitelist entry
   is `logged: true`, `run_log_path = /var/log/ansible-runs/<command_id>.log`
   is stored on `CommandState`; `{run_id}` placeholder resolves to `command_id`.
3. Pipeline runs `run-ansible.sh … --log-dir /var/log/ansible-runs --run-id <command_id>`.
   The script tees container stdout/stderr to that file on the control_node.
4. Client opens `GET /command/execution/<command_id>/view` → auto-refreshing
   HTML.
5. JS polls `GET /command/execution/<command_id>/trace/ui?byte_offset=N&line_num=M`
   every few seconds. Each poll SSHes to the control_node, `stat`s + `tail -c +N`,
   returns new lines + new offset. Page appends.
6. When `status` is terminal and offset == total_size, JS stops polling. The
   complete log remains on disk and re-viewable until a later run's
   self-cleaning prune (default: older than 3 days) removes it.
7. `GET /command/execution/<command_id>` still returns status/exit_status. For
   `logged` commands the control_node file is the primary log surface, so the
   poll endpoint no longer carries the full output (see "Output policy for
   logged commands" below). Non-`logged` commands keep their existing
   `output` behaviour unchanged.

## Output policy for logged commands

For `logged` commands the full output already lives on the control_node and is
viewable via `/view`. Storing the entire multi-MB log a *second* time in Redis
(the `CommandState.output` field) is redundant and wasteful, so we trim what the
poll endpoint persists — driven by the same `logged` flag, decided in the
service layer (not in any shell script):

- **Success (exit 0):** `output = None`. The user goes to `/view` for the log.
- **Failure (exit ≠ 0) or killed:** keep only the **last 50 lines** of the
  collected output as a short error summary, so `GET /execution/{id}` shows
  *why* it failed without forcing the user to open the viewer. The complete log
  remains on disk / in `/view`.
- **Non-`logged` commands:** unchanged — full `output` stored as today.

This is the single control point: `command_service` decides at result-storage
time (`_store_result` / `_handle_async_execution`) based on
`cmd_config.logged`. Success/failure determination itself is unaffected — it
still comes from the SSH process exit status, independent of whether we persist
the output. A new setting `COMMAND_LOG_FAILURE_TAIL_LINES` (default 50) controls
the failure-tail size; `0` would mean "store nothing even on failure".

## Error handling

- **Command id unknown:** `/trace/ui` → 404 (`NotFoundException`), same as the
  existing poll endpoint.
- **`run_log_path` is None:** return empty trace + status (command not logged).
- **Log file not yet created** (run just started, tee hasn't flushed): `stat`
  fails → treat as `total_size=0`, empty lines, status `running`. Viewer keeps
  polling.
- **SSH to control_node fails during a poll:** surface as
  `UpstreamUnavailableException` (502) / `UpstreamTimeoutException` (504),
  reusing the connect error mapping in `_connect`. The viewer JS already has a
  consecutive-failure cap (`MAX_FAILURES`) — it stops polling and shows an
  error after N failures, exactly like the deploy viewer.
- **Runaway log size:** `size_warning` (soft cap) shows a banner but keeps
  polling; `too_large` (hard cap) stops polling and tells the user the log is
  too large to stream — consistent with deploy behaviour.
- **Killed mid-run:** state goes `KILLED`; the partial log file is still on disk
  and viewable. JS sees terminal status and stops once caught up.

## Testing

Unit (mock the SSH/Redis repos, no real control_node):

- `get_command_trace` with `run_log_path=None` → empty trace, echoes status.
- `get_command_trace` happy path: given fake `stat`+`tail` outputs, asserts
  correct `lines`, `next_byte_offset`, `next_line_num`, `total_size`.
- Incremental poll: second call with the prior `next_byte_offset` only returns
  the newly-appended bytes.
- Soft-cap / hard-cap flags flip at the configured thresholds.
- Unknown command_id → `NotFoundException`.
- `_build_pipeline` resolves `{run_id}` for a `logged` command and stores
  `run_log_path`; does NOT inject it for a non-logged command.
- Anti-injection unaffected: `run_log_path` and `run_id` flow as discrete
  `shlex`-quoted args (assert the built command array, no shell metacharacters).

Integration (`TestClient`):

- `/trace/ui` requires `command_api` scope (401/403 without it).
- `/trace/ui` for an unknown id → 404 envelope.
- `/view` returns HTML containing the command's trace URL.

`run-ansible.sh`: a small bats/shell test (or manual `setup-ssh-nodes` run)
that `--run-id foo` writes `<log-dir>/foo.log` and rejects a bad `--run-id`.

## Settings (new)

| Setting | Purpose | Default |
|---|---|---|
| `COMMAND_LOG_DIR` | control_node dir for run logs (passed to `--log-dir`) | `/var/log/ansible-runs` |
| `COMMAND_LOG_SOFT_CAP_BYTES` | soft size cap → `size_warning` | (match deploy) |
| `COMMAND_LOG_HARD_CAP_BYTES` | hard size cap → `too_large` | (match deploy) |
| `COMMAND_LOG_FAILURE_TAIL_LINES` | lines of output kept on failure for `logged` commands (0 = none) | `50` |

## Open questions to confirm at implementation time

1. Does `get_formatted_job_trace`'s line-formatting helper cleanly separate from
   GitLab, or does it need extracting? (Prefer extracting a shared
   bytes→lines helper.)
2. Exact deploy viewer auth posture for `/view` — match it precisely.
3. Whether to reuse `command_id` as `run_id` (recommended) vs. a separate id.
   Recommendation: reuse `command_id`.
```
