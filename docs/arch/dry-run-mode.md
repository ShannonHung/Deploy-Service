# Dry-Run Mode — E2E Pipeline Testing

---

**Status:** Spec — ready for implementation (2026-10-06)
**Scope:** `deploy-service` (pipeline + SSH command) and `cluster-service` (deploy/command proxy)
**Tickets:** [Deploy-Service#32](https://github.com/ShannonHung/Deploy-Service/issues/32) · [Cluster-Service#20](https://github.com/ShannonHung/Cluster-Service/issues/20)

See `docs/arch/ssh-command.md` for the full design of the SSH command API (whitelist model,
anti-injection architecture, process-group tracking, Redis state machine). This document
covers only the dry-run mode layered on top of it and on the GitLab pipeline API.

---

## Problem Statement

A team wants to run an end-to-end pipeline test that exercises `cluster-service` and
`deploy-service` HTTP APIs without causing real side effects. As stated, the request was:
"the API shouldn't actually fire, but we need to confirm the response is 200 OK."

Today there is no way to do that. Every write-path endpoint in both services reaches a real
external system:

- `POST /api/v1/deploy/stage` triggers a real GitLab pipeline.
- `POST /api/v1/command/execute` opens a real SSH connection and runs a real command —
  including destructive ones such as `reboot`.
- `cluster-service`'s `/api/v1/deploy/...` and `/api/v1/command/...` proxy straight through
  to the above.

So an e2e test today either mutates production systems or cannot run at all. The test
suite that *does* exist is split between unit tests (which mock repositories) and
`e2e`-marked tests (which need live Redis / SSH / a cluster and are excluded from
`make test`). Neither gives a CI-safe way to walk the real HTTP surface end to end.

### The ambiguity that must be resolved first

"Return 200 without calling the API" can mean two very different things, and the choice
determines whether the resulting test has any value:

| | A. Contract test | B. Behaviour-free stub |
|---|---|---|
| Skips | Only the outermost side effect (GitLab trigger, SSH exec) | Everything |
| Still runs | Routing, JWT + scope checks, Pydantic validation, whitelist + anti-injection validation, duplicate detection | Nothing |
| Catches a regression in auth? | Yes | No |
| Catches a regression in request/response schema? | Yes | No |
| Catches a broken command whitelist? | Yes | No |

Interpretation B — short-circuiting at the router with `if dry_run: return 200` — produces a
test that **can never fail**. It would still pass with authentication removed, the response
schema broken, or the command whitelist bypassed. It tests one `return` statement.

**This spec implements interpretation A.** The implementation cost is the same; the
difference is whether the e2e test is a safety net or decoration.

A second, more serious concern specific to the command API: the per-user whitelist, argument
regex validation, and `shlex.join` positional-argument construction are a **security
boundary**. A dry-run that short-circuits before them creates a code path that bypasses every
input check *and reports 200*, which an e2e suite would then display as "security checks
passing." Interpretation A structurally prevents this, because the stub sits after validation.

## Solution

Introduce a deployment-level `DRY_RUN_MODE` flag. When enabled, both services keep their full
request-handling pipeline — auth, validation, business logic — and substitute only the
outermost side-effecting collaborator with a stub implementation that returns plausible,
contract-correct canned data.

The substitution happens at existing dependency-injection boundaries, so no router, service,
or domain model logic changes. This is the payoff of the existing Dependency Inversion
architecture: the side effects are already isolated behind abstractions.

Dry-run is selected by **environment variable only**, never by a request parameter, and the
app refuses to start if dry-run is combined with a production environment.

## User Stories

### E2E pipeline testing

1. As a platform engineer, I want to run the full e2e pipeline against a dry-run instance, so that CI can validate the API surface without triggering real GitLab pipelines.
2. As a platform engineer, I want dry-run requests to return HTTP 200 with a schema-valid body, so that my e2e assertions can check response shape, not just status.
3. As a platform engineer, I want JWT and scope enforcement to still apply in dry-run, so that an accidental removal of an auth dependency fails the e2e test.
4. As a platform engineer, I want Pydantic request validation to still apply in dry-run, so that a breaking request-schema change is caught by the e2e test.
5. As a platform engineer, I want the response envelope (`ApiResponse[T]` with `data` + `request_id`) to be identical in dry-run, so that client code paths are exercised unchanged.
6. As a platform engineer, I want `X-Coordination-ID` propagation to work in dry-run, so that request-id plumbing is covered by the e2e test.
7. As a platform engineer, I want to run the e2e suite in CI with no Redis-less or SSH-less skips for the dry-run paths, so that the suite reports real coverage rather than silently skipping.
8. As a platform engineer, I want both services runnable in dry-run simultaneously, so that the cluster-service → deploy-service proxy hop is exercised end to end.
9. As a platform engineer, I want the e2e test to exercise `cluster-service` against a *real* dry-run `deploy-service`, so that the proxy's URL construction and token management are genuinely tested.

### Pipeline (deploy) endpoints

10. As an e2e test, I want `POST /api/v1/deploy/stage` in dry-run to return a `PipelineData` with a recognisable fake id, so that I can assert on the response without a real pipeline existing.
11. As an e2e test, I want the `EXECUTION` variable injection from the `action` query param to still happen in dry-run, so that this load-bearing behaviour is covered.
12. As an e2e test, I want the `SERVICE_FROM` variable injection from the authenticated user to still happen in dry-run, so that caller-identity plumbing is covered.
13. As an e2e test, I want duplicate-pipeline detection logic to still execute in dry-run, so that a regression in `_variables_match` is detectable.
14. As an e2e test, I want dry-run duplicate detection to report no duplicates by default, so that `POST /stage` does not spuriously return 409.
15. As an e2e test, I want `POST /api/v1/deploy/stage/check-running` in dry-run to return a valid `RunningPipelinesData`, so that the endpoint's contract is verified.
16. As an e2e test, I want `GET /api/v1/deploy/stage/{id}` in dry-run to return a valid `PipelineData`, so that the status-poll contract is verified.
17. As an e2e test, I want `POST .../{id}/cancel` and `POST .../{id}/retry` in dry-run to return valid `PipelineData`, so that both mutation contracts are verified.
18. As an e2e test, I want the dynamic per-project token resolution (`project_id` → `GitlabAuthRepository`) to be bypassed in dry-run, so that dry-run needs no real GitLab credentials configured.
19. As a platform engineer, I want dry-run to require no `GITLAB_TOKEN`, so that the CI dry-run instance holds no production secrets.

### SSH command endpoints

20. As an e2e test, I want `POST /api/v1/command/execute` in dry-run to return a valid `CommandExecutionResponse`, so that the execute contract is verified without running a real command.
21. As a security-conscious engineer, I want the per-user whitelist file to still be loaded and enforced in dry-run, so that a whitelist regression fails the e2e test instead of passing silently.
22. As a security-conscious engineer, I want argument regex validation to still run in dry-run, so that validation coverage is not lost.
23. As a security-conscious engineer, I want the anti-injection check (`_validate_anti_injection`) to still run in dry-run, so that the security boundary is exercised.
24. As a security-conscious engineer, I want a command *not* on the user's whitelist to still be rejected with 403 in dry-run, so that the e2e suite proves the deny path works.
25. As a security-conscious engineer, I want a command with a disallowed host to still be rejected in dry-run, so that host allow/deny logic is covered.
26. As an e2e test, I want the pipeline-step construction (`List[List[str]]` + `shlex.join`) to still run in dry-run, so that the command-building logic is covered.
27. As an e2e test, I want host resolution (bastion / cluster / inventory lookup) behaviour in dry-run to be explicitly decided, so that dry-run does not depend on a live inventory API.
28. As an e2e test, I want capacity/backpressure gates (`COMMAND_MAX_CONCURRENCY`, `COMMAND_MAX_RUNNING`) to still apply in dry-run, so that the gate logic is covered.
29. As an e2e test, I want an async command in dry-run to return a `command_id` immediately, so that the async contract matches production.
30. As an e2e test, I want the Redis-backed command state machine (`RUNNING → SUCCESS`) to still execute in dry-run, so that state transitions are genuinely covered.
31. As an e2e test, I want `GET /api/v1/command/{id}` polling in dry-run to return a terminal state eventually, so that the poll loop terminates.
32. As an e2e test, I want `POST /api/v1/command/{id}/kill` in dry-run to transition state per the real state machine, so that the kill contract is verified.
33. As an e2e test, I want a `logged` command's `{run_id}` injection and log-path computation to still happen in dry-run, so that that logic is covered.
34. As an e2e test, I want the `READY` handshake path for detached runs to be satisfied by the stub, so that a dry-run `logged` command does not fail with a missing-handshake error.
35. As an e2e test, I want a `disconnects_ssh: true` command (e.g. reboot) in dry-run to take the fire-and-forget path and return successfully, so that the most destructive endpoint is testable with zero risk.
36. As an e2e test, I want the script-version precheck to be satisfied (not fail) in dry-run, so that dry-run does not require a real target script.
37. As an e2e test, I want dry-run to produce some stub log output, so that the log-viewer / trace endpoints return non-empty content.

### cluster-service proxy

38. As an e2e test, I want cluster-service's deploy proxy endpoints in dry-run to return valid responses, so that the proxy contract is verified.
39. As an e2e test, I want cluster-service's command proxy endpoints in dry-run to return valid responses, so that the command proxy contract is verified.
40. As an e2e test, I want cluster-service's `DeployServiceError` mapping (`_DEPLOY_CODE_MAP` / `_DEPLOY_STATUS_MAP`) to remain reachable in dry-run, so that error adaptation is still testable.
41. As a platform engineer, I want cluster-service's Kubernetes node endpoints in dry-run to not touch a real cluster, so that cordon/drain are safe to include in an e2e run.
42. As a platform engineer, I want cluster-service dry-run to require no kubeconfig, so that the CI instance needs no cluster credentials.

### Safety

43. As an operator, I want dry-run to be selectable only by environment variable, so that no API caller holding a valid token can make production silently no-op.
44. As an operator, I want the app to refuse to start when `DRY_RUN_MODE=true` and `APP_ENV=prod`, so that a misconfigured production deploy fails loudly at boot rather than silently.
45. As an operator, I want a prominent WARNING log line at startup when dry-run is active, so that the mode is visible in logs.
46. As an operator, I want every dry-run response to carry a `dry_run: true` marker, so that a "successful" response cannot be mistaken for real work having happened.
47. As an operator, I want the marker to default to `false` and be omitted-safe for existing clients, so that adding it is not a breaking change.
48. As an operator, I want dry-run fake identifiers to be obviously synthetic, so that a leaked dry-run value fails fast downstream rather than colliding with a real record.
49. As an operator, I want `/health` and readiness behaviour unchanged in dry-run, so that orchestration treats the instance normally.
50. As an operator, I want the Swagger/OpenAPI schema in dry-run to be identical to production, so that the e2e test validates the same contract clients consume.
51. As a developer, I want dry-run to be documented in both services' `CLAUDE.md`, so that the next contributor does not confuse it with the existing router-level `drain` dry-run.

## Implementation Decisions

### Seam selection — one injection point per service area

Three seams, all of them **existing** DI boundaries. No new seams are introduced.

| Area | Seam | Substituted at |
|---|---|---|
| deploy-service pipeline | `PipelineRepository` (ABC) | `_get_deploy_service()` factory in the deploy router |
| deploy-service command | `CommandExecutor._connect` | returns a fake connection object |
| cluster-service proxy | `DeployServiceClient` | `_get_pipeline_service()` / command-service factory |
| cluster-service k8s | `KubeClientFactory` → `CoreV1Api` | fake `CoreV1Api`-shaped object |

**Why `_connect` is the right seam for the command API.** `execute_command` runs in this order:

1. `_check_capacity` — backpressure gate
2. `_prepare_execution` — whitelist load, argument regex, anti-injection, host resolution
3. `_pipeline_builder.build` — `List[List[str]]` construction
4. `_connect` — SSH session
5. `_handle_fire_and_forget` / `_handle_async_execution`

Every validation and security check happens in steps 1–3, strictly *before* step 4. Replacing
only step 4's return value therefore preserves the whole security boundary for free. This is
the highest seam that is still below all validation — a seam any higher (router, service
entry) would bypass the checks the e2e test most needs to cover.

### The fake SSH connection

The stub must satisfy exactly the `asyncssh` surface the executor consumes. Audited, that
surface is four members:

- `conn.run(cmd, check=False)` — used by the version precheck and the fire-and-forget preview
- `conn.create_process(cmd, stdin=, stdout=, stderr=)` — used by the pipeline executor
- `conn.is_closed()` — used to detect fire-and-forget disconnection
- `conn.close()`

The process object returned by `create_process` must additionally provide `stdout`, `stderr`
(with `readline()`), and `wait()`. Two `stderr.readline()` reads are protocol-significant:

1. The first line is parsed as a **PGID** (`int`) — the stub must emit a plausible integer.
2. For a `detached` (`logged`) run, the second line must be exactly `READY`, or the executor
   raises `CommandExecutionException`.

The fake must honour both, otherwise `logged` commands fail in dry-run. For a
`disconnects_ssh: true` command the fake must report `is_closed() == True` after the run so
the fire-and-forget path is taken.

This keeps both handlers — and therefore the Redis state machine, log tailing, and the
`RUNNING → SUCCESS` transitions — genuinely executing against the stub.

### The dry-run repository / client stubs

- `DryRunPipelineRepository` implements `PipelineRepository` in full. `list_running` returns
  an empty list, so duplicate detection executes its real comparison logic but never
  produces a spurious 409. `trigger` / `get` / `cancel` / `retry` return `PipelineData` with a
  synthetic id.
- `DryRunDeployServiceClient` (cluster-service) mirrors `DeployServiceClient`'s public
  methods, returning the same domain models.
- Both live alongside their real counterparts in `repositories/` and `clients/`, following
  the existing naming convention.

### Configuration and safety

- A single `DRY_RUN_MODE: bool = False` setting in each service's `Settings`, read from the
  environment like every other setting. **Not** a query parameter, header, or request-body
  field — a request-level switch would let any token holder make production silently no-op.
- `create_app()` raises at startup if `DRY_RUN_MODE` is true while `APP_ENV == "prod"`. A
  hard failure, not a warning, because warnings get ignored.
- A prominent WARNING log line on startup when dry-run is active.
- Dry-run needs no GitLab token, SSH key, inventory credentials, or kubeconfig — a CI
  instance holds no production secrets.

### Response marker

Add an optional `dry_run: bool = False` field to the `ApiResponse` envelope, set true in
dry-run. Default-false keeps it non-breaking for existing clients. Rationale: when someone
produces a 200 response and asks why the machine never rebooted, the answer should be
printed in the payload.

### Synthetic identifier convention

Dry-run identifiers are deliberately implausible as real records (e.g. a pipeline id in a
reserved high range such as `999001`, a `command_id` carrying a `dryrun-` prefix) rather than
realistic-looking values. A leaked dry-run id should break loudly downstream, not propagate
quietly.

### Host resolution in dry-run

Host resolution reaches the inventory API, an external dependency. Decision: dry-run
substitutes the `InventoryRepository` seam too (it is already an ABC injected via
`get_inventory_repository`), returning a canned resolution. This keeps host-type and
bastion-resolution *logic* executing while removing the network dependency.

### Relationship to the existing `drain` dry-run

`cluster-service`'s `drain` endpoint already has a `dry_run` request field, resolved by a
router-level short-circuit. That is interpretation B, and it is **a different mechanism with
different semantics**: per-request, caller-controlled, and specific to one endpoint. This
spec's `DRY_RUN_MODE` is deployment-level and service-wide. Both are kept; neither changes
the other. The distinction must be documented in `CLAUDE.md` so future contributors do not
conflate them or "unify" them into a request-level flag.

## Testing Decisions

### What makes a good test here

Test external behaviour through the HTTP surface, not the stub's internals. A test asserting
"`DryRunPipelineRepository.trigger` was called" tests wiring; a test asserting "`POST
/api/v1/deploy/stage` returns 200 with a schema-valid `PipelineData` and `dry_run: true`"
tests behaviour. Prefer the latter.

The most valuable tests here are the ones proving dry-run **did not** weaken anything:
the deny paths. A dry-run test suite that only checks happy-path 200s reproduces exactly the
false confidence this spec exists to avoid.

### Modules under test

- **Integration (`tests/integration/`, full `TestClient`)** — the primary layer, since the
  point is the HTTP contract:
  - every deploy endpoint returns 200 with a valid envelope in dry-run
  - every command endpoint returns 200 with a valid envelope in dry-run
  - missing/invalid token still 401; missing scope still 403
  - a non-whitelisted command still 403
  - a disallowed host still rejected
  - an invalid request body still 422
  - `dry_run: true` present in dry-run, absent/false otherwise
  - a `logged` command completes (proving the `READY` handshake stub works)
  - a `disconnects_ssh` command takes the fire-and-forget path
  - async execute → poll reaches a terminal state
  - kill transitions state correctly
- **Unit (`tests/unit/`)**:
  - `DryRunPipelineRepository` satisfies the `PipelineRepository` contract (every abstract
    method implemented, return types correct)
  - the fake connection satisfies the four-member `asyncssh` surface, PGID line, and `READY`
    line
  - startup refuses `DRY_RUN_MODE=true` + `APP_ENV=prod`
  - `get_settings.cache_clear()` used when toggling the flag between tests

### Prior art in this codebase

- `tests/integration/test_deploy_dynamic_auth.py` — integration pattern for deploy routes
  with auth variations.
- `tests/integration/test_command_kill_api.py`, `test_command_running_api.py`,
  `test_command_trace_api.py` — `TestClient` patterns for the command API including state
  transitions.
- `tests/unit/test_command_service_errors.py`, `test_whitelist_*.py` — the deny-path
  assertions this spec's security tests should mirror.
- `cluster-service` injects a fake `CoreV1Api`-shaped object into `NodeService` rather than
  patching `kubernetes.client`. The fake-SSH-connection approach here follows the same
  philosophy: fake at the collaborator boundary, do not monkeypatch the SDK.
- `tests/conftest.py` sets `APP_ENV=test` before any app import; dry-run tests must set
  `DRY_RUN_MODE` the same way and clear the settings cache.

### E2E marker discipline

Per this project's convention, any test requiring live Redis, SSH, Docker, or a cluster must
be marked `@pytest.mark.e2e` or it passes locally and breaks CI (`make test` runs
`-m 'not e2e'`). Dry-run integration tests that need **only** Redis still need the marker;
the goal is for most dry-run tests to need nothing external and therefore run in `make test`.
Whether the command-path dry-run tests can avoid Redis entirely depends on the state
repository, and should be confirmed during implementation — if Redis is unavoidable, those
tests are `e2e`-marked and a Redis-free subset is kept in `make test`.

## Out of Scope

- **Validating that the GitLab pipeline itself works.** Dry-run proves *this service* behaves
  correctly; it cannot prove the downstream pipeline does. This was raised with the
  requesting team and **confirmed as acceptable (2026-10-06): what they care about is that
  our service behaves correctly**, not that the GitLab pipeline runs. If that requirement
  ever changes, dry-run is the wrong tool and a staging GitLab project with real triggers
  would be required instead.
- Record/replay or VCR-style fixture capture of real upstream responses.
- Configurable or scripted dry-run responses (e.g. "make this request return 500"). Fault
  injection is a separate concern; the stubs here return success-shaped data only.
- Changing the existing `drain` request-level `dry_run` behaviour.
- A dry-run mode for the `inventory` fake-api sub-project.
- Performance or load testing.
- Any new HTTP endpoint.

## Further Notes

The reason this is a small change is that the architecture already earned it: services depend
on ABCs, side effects are isolated in repositories and clients, and the DI factories are
single chokepoints. The work is mostly writing the stubs — not rewiring anything.

The ordering risk worth flagging: `_connect`'s position *after* all validation is what makes
the command seam safe. If a future refactor moves validation after connection, or moves
`_connect` earlier, the dry-run path silently becomes a validation bypass. A comment at
`_connect` noting that dry-run depends on this ordering is cheap insurance.

Suggested implementation order, lowest risk first: deploy-service pipeline → cluster-service
proxy → deploy-service command (the fake SSH connection is the fiddliest piece and benefits
from the pattern being settled first).

A cross-repo note: this spec lives in `deploy-service` and covers both services. The
`cluster-service` portion is tracked by a companion issue in that repo pointing here.

---

## Ticket Breakdown

Eleven tracer-bullet tickets. The two chains are fully independent and can run in parallel.

```
deploy-service:                  cluster-service:
T1 ─┬─ T2 ──────────┐            T8 ─┬─ T9 ───┐
    ├─ T3 ─┐        │                └─ T10 ──┴─ T11
    └──────┴─ T4 ─┬─ T5 ─┤
                  └─ T6 ─┴─ T7
```

### deploy-service

| # | Ticket | Blocked by |
|---|---|---|
| T1 | Prefactor: `DRY_RUN_MODE` setting, prod startup refusal, `dry_run` response marker | — |
| T2 | Dry-run pipeline repository — deploy endpoints return canned `PipelineData` | T1 |
| T3 | Dry-run inventory repository — host resolution without the inventory API | T1 |
| T4 | Fake SSH connection — synchronous command execution | T1, T3 |
| T5 | Dry-run async command lifecycle — poll and kill | T4 |
| T6 | Dry-run `logged` and fire-and-forget commands | T4 |
| T7 | Deny-path coverage and `CLAUDE.md` documentation | T2, T5, T6 |

### cluster-service

| # | Ticket | Blocked by |
|---|---|---|
| T8 | Prefactor: `DRY_RUN_MODE` setting, prod startup refusal, `dry_run` marker | — |
| T9 | Dry-run deploy-service client — deploy and command proxy endpoints | T8 |
| T10 | Fake `CoreV1Api` — node endpoints without a real cluster | T8 |
| T11 | Deny-path coverage and `CLAUDE.md` documentation | T9, T10 |

### Notes on the slicing

**T1 / T8 stub nothing.** They install the configuration and safety scaffolding only; with
`DRY_RUN_MODE=true` the services still reach GitLab and SSH exactly as before. The value is
getting the guard rails in before any behaviour is substituted.

**The `dry_run` marker is set in one place.** There are 52 `ApiResponse(...)` construction
sites across the two services. Rather than a wide refactor across all of them, the field is
added to the envelope with a `False` default and resolved once from settings — zero call-site
churn, no expand–contract sequence needed.

**T4 is deliberately narrow.** The fake SSH connection is the fiddliest piece of this work
(the PGID line and the `READY` handshake are protocol-significant), so T4 covers only the
simplest synchronous path. Async/kill (T5) and `logged`/fire-and-forget (T6) build on it once
the pattern is settled.

**T7 / T11 are the point, not an afterthought.** A dry-run suite that only asserts happy-path
200s reproduces exactly the false confidence this document exists to prevent. The deny paths
(401 / 403 / 422, non-whitelisted command, disallowed host) are what prove dry-run weakened
nothing.
