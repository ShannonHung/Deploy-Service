# Ansible Runner — Known Risks & Future Work

Status: notes / not-yet-implemented. Captured 2026-06-24 from a design review of
the `run_ansible*` command path (HTTP → `CommandService` → SSH control_node →
`run-ansible.sh` → `docker run` ansible-runner → `ansible-playbook` → nodes).

## The trigger chain is long and synchronous

A single request needs five external dependencies healthy **at once**:
control_node SSH, GitLab (inventory clone), the Docker registry (image pull),
the Docker daemon, and the target nodes' SSH. Any one being slow or down fails
or stalls the request, and the failure is buried in ansible stdout — the service
only gets an exit code, so "GitLab down" vs "node down" vs "bad playbook" all
look the same. This is the root cause behind several of the items below.

## 1 + 2. (Resolved) Orphaned runs: log file is now the source of truth

**What was always fine:** the log viewer (`/view` → `/trace/ui`) and cross-pod
`kill` work from any pod, because they rebuild everything from Redis
(`CommandState`: `run_log_path`, `resolved_ip`, `pgids`, `username`) and SSH back
to the control_node. Reading the log is stateless. ✓

**What used to break — two separate holes:**

- **(state)** Deciding "did it finish / succeed / fail" was owned by a single
  `asyncio.Task` in the pod that started the run. If that pod died, no one wrote
  the final result to Redis, so `CommandState` was stuck at `RUNNING` until TTL.
- **(survival)** Worse: the run didn't even keep running. A logged run's
  stdout/stderr flowed back over the SSH channel (`asyncssh.PIPE`). When
  deploy-service died, the channel closed → `tee` got **SIGPIPE** → the SIGPIPE
  cascade killed docker/ansible mid-run (exit **141** = 128+13). `setsid` gives
  a new pgid (good for kills) but does NOT detach the inherited stdout fd, so it
  didn't help here. The `killable` flag is irrelevant — the run dies purely
  because its output is plumbed through the dying channel.

### Implemented fix

0. **Detach logged runs from the SSH channel so they SURVIVE** (the survival
   hole). For logged commands `_build_step_wrapper` now wraps the step as
   `setsid -w sh -c 'echo $$ >&2; echo READY >&2; exec "$@" > /dev/null 2>&1 < /dev/null' _ …`:
   - `exec … > /dev/null 2>&1 < /dev/null` severs stdout/stderr/stdin from the
     channel, so closing it can't SIGPIPE the run. The run **script** still
     `tee`s to the log file; we redirect to `/dev/null` (not the log path) so we
     don't double-write the file the script owns.
   - Two-line stderr handshake **before** exec — PGID then `READY` — still
     reaches the channel. `_execute_pipeline` waits for `READY`; if it never
     arrives the command died before exec (script not found, log dir
     unwritable), so we fail fast with `CommandExecutionException` instead of
     hanging in RUNNING (blind-spot B). Non-logged commands are unchanged
     (output still streams back; their output IS the result).

1. **`run-ansible.sh`** now records the real ansible exit code after the run
   (captured via `${PIPESTATUS[0]}`, since ansible is the left side of the
   `| tee` pipe; `set +e`/`set -e` around it so a failure still reaches the
   marker, then `exit $RUN_EXIT` so the fast path still sees the true status):
   - appends `=== EXIT <code> ===` to the log (human-visible in `/view`), and
   - writes a `<run_id>.exit` sidecar atomically (`tmp` + `mv`), only when a
     `--run-id` was supplied (i.e. a deploy-service run).
2. **`get_command_execution_result`** (poll endpoint): if Redis is in a stuck
   transient state (`RUNNING` **or** `KILLING`) **and** the command is `logged`
   (`run_log_path` set), it SSHes back via `_read_run_exit_marker` and reads the
   sidecar:
   - absent/unparseable → still in flight, report the last-known state.
   - `EXIT 0` → `_heal_from_marker` → `mark_success`.
   - `EXIT != 0` → `mark_failed` (exit code surfaced).
   The heal uses `update_if(condition=status in {RUNNING, KILLING})`, so a
   concurrent fast-path write or a completed `kill` (which lands on the terminal
   `KILLED`) always wins the race and is never overwritten (`KILLED`/`SUCCESS`/
   `FAILED` are never resurrected). SSH failures during a heal are swallowed —
   a transient control_node outage degrades to the last-known state, never a 5xx.

   **Why `KILLING` is healed too** (found in manual testing): a `killable:false`
   run, when the service shut down, was flipped to `KILLING` by
   `kill_command`/`shutdown_gracefully` and then stranded there — it has no kill
   path to reach `KILLED`. Two fixes: (a) `kill_command` now refuses a
   non-killable command **before** any state transition (leaves it `RUNNING`),
   and (b) the heal recovers `KILLING` so even a kill interrupted by a dying pod
   reconciles to the real outcome.
3. The `asyncio.Task` is now an **optimisation** (fast path), not the only
   writer. Any pod, any time — even after a full service restart — recovers the
   true outcome from the control_node.

### `killable` semantics: system-automatic vs human override

`killable: false` means **"the system must not kill this on its own"**, not
"no one may ever kill this". The two callers are treated differently:

- **Automatic** (`_timeout_wrapper`, `shutdown_gracefully`) call
  `kill_command(...)` with no `force` → always respect `killable`.
- **Human** (`POST /execution/{id}/kill`): a plain kill of a `killable:false`
  command returns **409** (`"not killable. Retry with ?force=true"`) instead of
  a misleading `accepted` (found in manual testing — the endpoint used to return
  `accepted` while the service silently did nothing). An explicit
  `?force=true` forwards `force=True` to `kill_command`, which bypasses the
  killable guard and performs the real PGID kill. `disconnects_ssh` (reboot)
  commands can't reach this path — they're fire-and-forget and never hold a
  `RUNNING` async state, so the top-level `status != RUNNING` check rejects them.

Reuses the viewer's "SSH back and read a file" pattern (shared
`_connect_to_control_node` helper). Tests: `tests/unit/test_command_orphan_heal.py`
and the marker tests in `tests/integration/test_run_ansible_script.py`.

### Still per-pod (not addressed here)

`COMMAND_MAX_RUNNING` / `COMMAND_MAX_CONCURRENCY` remain **per-pod**, not global
(N pods → N× the cap). Genuinely global backpressure would need a shared counter
(e.g. Redis) and is out of scope for the orphan fix.

## 3. (Deferred by the user) per-run `git clone` + `docker pull`

Every trigger clones the inventory repo and pulls the image, binding "can I run
ansible" to two services we don't control. Future: cache/pin the image, shallow
or cached inventory. Low priority for now.

## 4. (Resolved) playbooks are baked into the image

`ansible/Dockerfile` does `COPY playbooks /playbooks`, so adding a playbook means
`make build` (+ `make push` for prod), not a host mount. The earlier
`--playbook-dir` bind-mount experiment was reverted — it only made sense for
fast local iteration and risked a wrong host path under Docker-out-of-Docker.

## 5. (Resolved) playbook is a regex-restricted argument

A single generic `run_ansible` whitelist entry takes the playbook as an argument
validated by regex (`^(ping|fail|clock)\.yml$`) — the only gate on which
playbook runs, since the name is passed straight to `ansible-playbook`. Adding a
playbook = bake it into the image + extend the regex. Optional args
(`required: false`, e.g. `--limit`) let one entry serve playbooks with differing
needs; `run_ansible_clock` stays separate for its tunables.

### Residual security note (not yet addressed)

Running an arbitrary (whitelisted) playbook = root on every node. The argument
anti-injection (shlex) protects parameter values, but **playbook content itself
is unreviewed**. Whoever can write the image's `playbooks/`, edit the whitelist,
or control the inventory repo effectively controls the fleet. Worth a follow-up:
who can change those, and signing/review of the runner image.
