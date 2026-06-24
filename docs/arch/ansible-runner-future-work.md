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

## 1 + 2. Orphaned runs: state lives in one pod's memory  ← highest priority

**What is fine today:** the log viewer (`/view` → `/trace/ui`) and cross-pod
`kill` already work from any pod, because they rebuild everything from Redis
(`CommandState`: `run_log_path`, `resolved_ip`, `pgids`, `username`) and SSH back
to the control_node. Reading the log is stateless. ✓

**What breaks:** deciding "did it finish / succeed / fail" is owned by a single
`asyncio.Task` living in the pod that started the run (it calls `_collect_output`
then writes the result to Redis). If that pod restarts or dies:

- `run-ansible.sh` keeps running on the control_node (`setsid`) and the log keeps
  growing — the work is NOT lost.
- But no one writes the final result back to Redis, so `CommandState` is stuck
  at `RUNNING` until its TTL expires. The run is "orphaned" from the API's view.
- `COMMAND_MAX_RUNNING` / `COMMAND_MAX_CONCURRENCY` are also **per-pod**, not
  global, so backpressure is weaker than it looks (N pods → N× the cap).

### Proposed fix: make the log file the source of truth

This is the user's own suggestion and it matches how the viewer already works
(SSH back and read a file). Plan:

1. `run-ansible.sh` writes a terminal marker when it finishes — e.g. append a
   final line `=== EXIT <code> ===` to the log, OR write a sidecar
   `<run_id>.exit` file containing the exit code. (Sidecar is cleaner to parse;
   the log marker is human-visible in `/view`. Could do both.)
2. `get_command_execution_result` (the poll endpoint): if Redis says `RUNNING`,
   SSH back and check the marker/sidecar:
   - no marker → still running.
   - `EXIT 0` → success; lazily heal Redis (`mark_success`).
   - `EXIT != 0` → failed; heal Redis (`mark_failed`, optionally with the log
     tail per the existing `_apply_output_policy`).
3. Result: any pod, any time (even after a full service restart) can recover the
   true outcome from the control_node. The `asyncio.Task` becomes an optimization
   (fast path) rather than the only writer.

Reuses the exact pattern the viewer already relies on. Independent of the
viewer/auth/whitelist work; do it as its own change.

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
