# CLAUDE.md — deploy-service

Guidance for Claude Code when working in `deploy-service/`. The repository-wide
architecture notes live in the top-level `CLAUDE.md`; this file covers what is
specific to this service and is versioned alongside its code.

## Dry-Run Mode

`DRY_RUN_MODE=true` lets an e2e pipeline exercise the real HTTP surface without real side effects: no GitLab pipeline is triggered, no SSH connection is opened, no inventory API is called. Full design in **`docs/arch/dry-run-mode.md`** — read that before changing anything here.

**What it replaces.** Only the outermost side-effecting collaborators, each at an existing DI boundary:

| Seam | Replaced by |
|------|-------------|
| `PipelineRepository` (GitLab) | `DryRunPipelineRepository` |
| `InventoryRepository` | `DryRunInventoryRepository` |
| `CommandExecutor._connect` | `DryRunSSHConnection` |
| `SshSupport._load_ssh_config` / `_connect_to_control_node` | synthetic config / fake connection |

**What still runs — all of it.** Routing, JWT auth, scope checks, request validation, the per-user command whitelist, argument regex validation, `shlex.join` positional-argument construction, host allow/deny checks, duplicate-pipeline detection, the Redis command state machine, and the `RUNNING → KILLING → KILLED` transitions.

This split is the whole point. The seam sits strictly **below** every validation step (`_check_capacity` → `_prepare_execution` → `build` → `_connect`), so the security boundary is preserved for free. **Do not move the short-circuit higher.** A router-level short-circuit answers 200 to everything, which makes the e2e suite pass just as happily with auth deleted or the whitelist bypassed — a test that reports "security checks passing" while proving nothing. `tests/integration/test_dry_run_contract.py` enforces this by test, not by comment: moving the short-circuit to the top of `execute_command` fails more than twenty tests across the dry-run suite, including every deny-path assertion.

**Enabling it.** Set `DRY_RUN_MODE=true` (environment variable only — never a request parameter, header or body field, since a per-request switch would let any token holder make a real deployment silently no-op). `DRY_RUN_COMMAND_SECONDS` makes a fake command take measurable time so the kill path is reachable; `0` (the default) finishes immediately.

The app **refuses to start** when `DRY_RUN_MODE=true` and `APP_ENV=prod` — a hard failure, not a warning. Every response carries `"dry_run": true` so a caller can never mistake a dry-run deployment for a production one.

**No production secrets required.** A dry-run instance needs no GitLab token, SSH private key, inventory credentials or kubeconfig. If a dry-run deployment appears to need one, something is reaching past a seam — treat it as a bug, not a config gap.

### Not to be confused with cluster-service's `drain` `dry_run`

`cluster-service` has a request-level `dry_run` field on its node-drain endpoint. The two are unrelated and **must not be merged or "unified" into a single request-level flag**:

| | cluster-service `drain.dry_run` | `DRY_RUN_MODE` |
|---|---|---|
| Scope | One endpoint | Whole service |
| Set by | The caller, per request | Deployment environment variable |
| Layer | Router short-circuit | Repository / connection injection |
| Purpose | "Validate this drain without performing it" | "Run the e2e suite without side effects" |

A router short-circuit is correct for `drain`: it is a caller-facing validation affordance on a single operation. It would be wrong for `DRY_RUN_MODE`, which has to leave the validation path intact in order to mean anything. They coexist without interacting.
