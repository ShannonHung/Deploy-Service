# GitLab job trace log viewer

**Status:** Implemented
**Date:** 2026-05-17
**Scope:** `deploy-service/`

## Problem

The job log viewer (`GET /api/v1/deploy/jobs/{job_id}/view`) needs to display a CI job's console output, which on long-running jobs can grow to tens of megabytes. The viewer polls the server every few seconds while the job is running, and a user may keep the page open for the entire duration of the job.

A naive implementation — server reads the full trace from GitLab on every poll, renders ANSI→HTML for the whole thing, returns it — fails on three axes at scale:

1. **Server-side cost**: re-fetching N megabytes from GitLab every 5 seconds, just to send a few new KB to the browser.
2. **Browser cost**: re-rendering the whole DOM on every poll. The viewer flickers and scrolls back to the top.
3. **GitLab cost**: every refresh on every open browser tab counts as one full trace download against the GitLab API.

The earlier implementation did exactly this. The current implementation eliminates all three problems via byte-offset incremental polling plus a Redis-backed terminal-job cache.

## Background

A few load-bearing facts about GitLab's job-trace API shaped this design:

- The trace is **append-only while the job is running**. Once the job reaches a terminal status (`success`, `failed`, `canceled`, `skipped`) the bytes never change again.
- The trace endpoint **does not honor HTTP Range headers**. We always receive the entire body from upstream regardless of what we ask for. (Verified during implementation; see `test_range.py` at the project root for the original probe script.)
- Traces are **ANSI-coded text** with GitLab-specific noise: timestamp prefixes, runner multiplexer marks (`00O+`), `section_start:` / `section_end:` markers, terminal `\x1b[0K` "clear-line" escapes. These need cleaning before HTML rendering.

## Goal

A log viewer that:

- Transfers only the **new** bytes between client and server on each poll, no matter how big the trace.
- Skips GitLab entirely once a job is finished (cache hit serves the whole tail).
- Renders incrementally — appends new lines without redrawing existing ones.
- Degrades gracefully under repeated failure (stops polling, surfaces enough info for the user to dig into GitLab directly).
- Refuses pathologically large traces before they OOM the pod or freeze the browser, while still pointing the user to GitLab where they *can* see them.

Non-goals:

- Live byte-by-byte streaming (we keep the polling model — see *Alternatives considered*).
- Persistent cross-process line state. Each browser session tracks its own offset.

## Design

### End-to-end flow

```
┌──────────┐  GET /trace/ui?byte_offset=N&line_num=M    ┌───────────┐
│ Browser  │ ───────────────────────────────────────►   │ FastAPI   │
│ (viewer) │                                            │ router    │
└──────────┘  ◄──── {lines: [...], next_byte_offset,    └─────┬─────┘
                          next_line_num, status}              │
                                                              ▼
                                                    ┌────────────────┐
                                                    │ DeployService  │
                                                    │ .get_formatted │
                                                    │   _job_trace   │
                                                    └────────┬───────┘
                                                             ▼
                                                ┌───────────────────────┐
                                                │ Repo.get_job_trace_   │
                                                │  range(job, offset)   │
                                                │                       │
                                                │  ┌─────────────────┐  │
                                                │  │ 1. Redis cache  │  │ ─► hit  ── slice & return
                                                │  │    lookup       │  │
                                                │  └────────┬────────┘  │
                                                │           ▼ miss      │
                                                │  ┌─────────────────┐  │
                                                │  │ 2. GitLab fetch │  │ ─► full body
                                                │  │    (full body)  │  │
                                                │  └────────┬────────┘  │
                                                │           ▼           │
                                                │  ┌─────────────────┐  │
                                                │  │ 3. Slice from   │  │
                                                │  │    byte_offset  │  │
                                                │  └────────┬────────┘  │
                                                │           ▼           │
                                                │  ┌─────────────────┐  │
                                                │  │ 4. If terminal: │  │
                                                │  │    write cache  │  │
                                                │  └─────────────────┘  │
                                                └───────────────────────┘
```

### Layered responsibilities

| Layer | File | Responsibility |
|---|---|---|
| Viewer (HTML/JS) | `app/core/log_viewer_template.py` | Tracks `byte_offset` + `line_num`. Polls with backoff. Appends only new lines. Trips an error panel after `MAX_FAILURES` consecutive failures. |
| Router | `app/api/v1/deploy.py:202` (`get_formatted_job_trace`) | Validates `byte_offset ≥ 0`, `line_num ≥ 1`. Injects `TraceCacheRepository`. |
| Service | `app/services/deploy_service.py:129` (`get_formatted_job_trace`) | Snaps trailing partial line off (held back for next poll), invokes the renderer with the correct starting line number, returns `FormattedLogResponse`. |
| Repository | `app/repositories/gitlab_pipeline_repository.py:269` (`get_job_trace_range`) | Cache lookup → GitLab fetch on miss → local slice → cache write on terminal status. |
| Cache | `app/repositories/trace_cache_repository.py` (`RedisTraceCache`) | Gzip-compressed `status\n + raw_trace_bytes` in Redis, keyed by `gitlab:trace:{project_id}:{job_id}`. |
| Renderer | `app/core/log_renderer.py` (`LogRenderer.render`) | ANSI→HTML, strips GitLab noise, simulates `\r` cursor moves. Accepts `start_line_num` so mid-stream slices stay numbered correctly. |

### The request/response protocol

The client maintains two integers across polls:

- **`byte_offset`** — total bytes of the trace already consumed by this client.
- **`line_num`** — line number to assign to the next rendered line.

Each poll sends both. The server returns:

```python
class FormattedLogResponse(BaseModel):
    job_id: int
    status: str               # GitLab job status; viewer stops polling on terminal
    next_byte_offset: int     # echoed back on next request
    next_line_num: int        # echoed back on next request
    lines: list[FormattedLogLine]
```

Crucially, `next_byte_offset` is **not always `byte_offset + len(new_bytes)`**. If the new bytes end mid-line (no trailing `\n`), the service trims the partial line off, renders only the complete lines, and reports `next_byte_offset` as the position of the last newline. The trimmed partial line gets re-fetched and completed on the next poll. This is what prevents the viewer from ever showing a half-written line.

### Why server-side slicing despite no Range support

GitLab ignores the `Range` header on the trace endpoint, so we cannot avoid downloading the full body from upstream on a cache miss. We still slice by `byte_offset` server-side because:

- The **client → server** hop is what the user feels (page latency, bytes over their network). Cutting it from "full trace every poll" to "delta every poll" is the visible win.
- The **server → GitLab** hop happens once per poll regardless. Slicing locally costs ~zero relative to the network fetch.
- It keeps the protocol symmetric with future improvements (e.g. if a future GitLab version honors Range, only the repo changes).

### Why a Redis cache, scoped to terminal jobs only

A finished job's trace is **immutable**. Caching it lets us:

- Serve the entire viewer session for a finished job (which is the common case — users reviewing past pipelines) with **zero** GitLab requests after the first miss.
- Share the cache across all FastAPI workers and across all users viewing the same job.

Running jobs are deliberately **not** cached. Their trace grows continuously; a 5-second-old cache entry would mean the viewer freezes 5 seconds behind reality until TTL expiry. This is the right tradeoff — running-job polls are bounded by the job's runtime, finished-job polls are bounded only by user attention spans.

The cache layout co-locates status with bytes (`gzip(status + "\n" + raw)`) so a hit needs zero GitLab calls — not even a status check. Gzip is worth the CPU because CI traces compress 80–90% (lots of repeated whitespace, escape sequences, and runner output).

### Why incremental rendering on the client

The renderer can't keep state across requests (each request is independent and might be served by a different worker), so the **line number** would normally reset to 1 on every call. That would either re-render the whole log every poll, or produce duplicate "line 1" entries on each new batch.

The fix: the response carries `next_line_num` and the client echoes it on the next request. The renderer takes `start_line_num` as input and assigns `start_line_num + i` to each line. Numbering stays continuous across polls without server state.

The JS side just calls `table.insertAdjacentHTML('beforeend', ...)` with the new lines. No re-render, no flicker, no scroll jump.

### Viewer failure mode

If the server returns non-2xx, malformed JSON, or the fetch errors, the viewer increments a `consecutiveFailures` counter. Below the cap (`MAX_FAILURES = 5`), the sync indicator shows `Sync Failed (N/5)` and polling continues with the existing backoff. Above the cap, `showFatalError()` clears the polling timer permanently, turns the indicator red, and replaces the log table with an error panel showing:

- `project_id` and `job_id` (for the user to look up in GitLab).
- The last error message.
- A best-effort direct link to the GitLab job (`{gitlab_url}/-/project/{project_id}/-/jobs/{job_id}`).

This prevents a misconfigured viewer from hammering GitLab indefinitely while still giving the user enough information to investigate.

### Size caps: soft warning + hard cutoff

Even with incremental polling and caching, an unbounded trace can still hurt us:

- **Pod memory.** On a cache miss the full trace lives in process memory three times over (raw bytes → decoded string → sliced tail). At 256 MB pod limits, a single 50 MB trace can OOM the worker.
- **Browser memory.** Rendering 100k+ DOM rows makes Chrome stall on scroll and search. Past ~50k lines the tab can crash.
- **GitLab traffic.** A single 50 MB live trace polled every 5 seconds is 10 MB/s of upstream traffic per viewer.

The naive fix — request coalescing across concurrent viewers of the same job — is complex (cross-pod locks needed) and doesn't solve the root problem (one viewer can still pull a 50 MB trace). We chose instead to *cap the worst case* with two thresholds:

| Cap | Default | Behavior |
|---|---|---|
| Soft (`GITLAB_TRACE_SOFT_CAP_BYTES`) | 5 MB | Response carries `size_warning=true`. Viewer renders a one-shot banner above the log: *"Log is large (5.2 MB). Rendering many lines may slow your browser — for better performance, view in GitLab."* with a direct link. Polling continues normally. |
| Hard (`GITLAB_TRACE_HARD_CAP_BYTES`) | 10 MB | Response carries `too_large=true` and **no `lines`**. Viewer calls `showFatalError({reason: 'too_large', ...})`: stops polling, appends an error panel below already-rendered lines (so the user doesn't lose their reading position), displays the trace size, project/job IDs, and a GitLab deep link. Status badge flips to `TOO LARGE`. |

The shape of `FormattedLogResponse` carries both flags plus `total_size` so the viewer can render the actual measurement, not the cap value:

```python
class FormattedLogResponse(BaseModel):
    # ...existing fields...
    total_size: int = 0       # current trace size, for UI display
    size_warning: bool = False  # soft cap crossed
    too_large: bool = False    # hard cap crossed — viewer must stop polling
```

The hard-cap check runs *before* the partial-line snap and renderer call (`deploy_service.py:get_formatted_job_trace`), so a too-large trace short-circuits with zero render cost. The first fetch still pays for the full body download (we have no way to know the size without downloading it — GitLab's HEAD support is undocumented), but the viewer immediately stops polling once it sees `too_large`, so subsequent polls don't happen.

Two design choices worth flagging:

1. **The hard-cap response keeps `next_byte_offset = byte_offset`** (no advance). This is moot because the viewer stops polling, but it means if some future client *doesn't* respect `too_large` and keeps polling, it won't appear to "make progress" past the cap.
2. **The error panel is appended, not replacing the log.** When the cap trips mid-stream (e.g. user is reading at 8 MB and the trace grows past 10 MB), wiping the rendered lines would be frustrating. Appending below preserves what the user was looking at.

### Configuration

In `app/core/config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `GITLAB_TRACE_TIMEOUT_SECONDS` | `45` | Per-fetch wall-clock timeout. Mapped to `UpstreamTimeoutException` (504-ish). |
| `GITLAB_TRACE_CACHE_TTL_SECONDS` | `86400` (24h) | How long a terminal-job trace stays in Redis. Long because immutable bytes are cheap to keep. |
| `GITLAB_TRACE_SOFT_CAP_BYTES` | `5 * 1024 * 1024` (5 MB) | Trace size above which the viewer shows a "view in GitLab" banner. Sized to the point where browser DOM rendering starts to feel sluggish. |
| `GITLAB_TRACE_HARD_CAP_BYTES` | `10 * 1024 * 1024` (10 MB) | Trace size above which the service stops returning lines and the viewer hands off to GitLab. Sized to the 256 MB pod memory budget (each in-flight trace consumes ~3× its size in RAM during decode + slice). |

Viewer-side constants (in `log_viewer_template.py`):

| Constant | Default | Purpose |
|---|---|---|
| `MAX_FAILURES` | `5` | Consecutive poll failures before the fatal error panel trips. |
| `MAX_INTERVAL` | `30000` ms | Upper bound on poll backoff for an active job emitting no new lines. |

## Pros

- **Bandwidth scales with growth rate, not log size.** A 50 MB log emitting 200 B/s costs ~1 KB per poll regardless of how big it gets.
- **Zero GitLab traffic for finished-job replays.** The common "look at yesterday's pipeline" case is fully cache-served after the first user opens it.
- **No flicker, no scroll jump.** Incremental DOM appends preserve scroll position; user can read mid-log without being yanked around.
- **Bounded blast radius on failure.** Polling stops automatically after `MAX_FAILURES`; no runaway browser tab can DOS GitLab through us.
- **Size caps protect both pod and browser.** The soft cap warns the user before their browser starts to struggle; the hard cap stops the service from OOMing the pod on a pathologically large trace.
- **Cache is process-shared and worker-shared.** Multiple FastAPI workers and multiple viewer tabs all benefit from one cache write.
- **Stateless server.** No per-session state means horizontal scaling is free; any worker can serve any poll.

## Cons / known limitations

- **Running jobs still re-download the full trace from GitLab on every poll.** GitLab's lack of Range-header support means we cannot make the upstream fetch incremental for active jobs. For a 50 MB live trace polled every 5 seconds, server-to-GitLab traffic is still ~10 MB/s per active viewer. We chose to live with this because (a) most traces never get that big, (b) it's GitLab's limitation not ours, and (c) it goes to zero the moment the job finishes.
- **Polling, not streaming.** Users see new lines on the next poll interval, not the instant GitLab flushes them. With 5s minimum interval, tail-following has up to 5s latency. A true streaming design (Server-Sent Events or long-lived HTTP) would be live but would force a substantial UI rewrite, in-browser ANSI parsing, and proxy timeout tuning. See *Alternatives considered*.
- **No resumption after page refresh.** Closing and reopening the viewer starts at `byte_offset=0`, which means one full re-fetch (cache-served if the job is terminal, GitLab-served otherwise). Persisting the offset in `localStorage` could fix this but adds complexity for marginal benefit.
- **Partial-line hold-back means up to one poll of latency on the very last line** of a still-flushing log section. Acceptable; the alternative (rendering half-written lines) is worse UX.
- **Cache key is `(project_id, job_id)` only.** Two jobs with the same ID across different GitLab instances would collide, but the deploy-service always points at one GitLab instance per deployment, so this is theoretical.
- **`get_trace_cache_repository` DI is only wired on the `/trace/ui` route.** Other endpoints that build the repo via `_get_deploy_service(project_id)` (without `trace_cache=`) get a repo with `trace_cache=None` and skip the cache entirely. This is fine — those endpoints don't call `get_job_trace_range` today — but worth knowing if you extend the repo to use the cache elsewhere.
- **Cache writes are best-effort.** A Redis outage during a `set()` is logged and swallowed (`gitlab_pipeline_repository.py:347`) so the user still gets their log; the next poll will retry the write. No retry queue.
- **Hard-cap fetches still pay the upstream cost once.** Because GitLab doesn't expose a way to size-check without downloading, the request that first crosses the hard cap *does* download the full body — it just doesn't render it. Subsequent polls don't happen (viewer stops). For a 50 MB log the first viewer eats one 50 MB GitLab fetch; later viewers of the same job skip the fetch entirely (cache hit if terminal) or also trip the cap and stop (running).
- **Hard cap is a single global value, not per-tier.** A power user who *wants* to view a 30 MB log inside the viewer has no opt-out short of editing config. Acceptable for now; if it bites, add a query param + scope-gated bypass.

## Alternatives considered

| Alternative | Why we didn't | Cost to switch later |
|---|---|---|
| Live streaming via SSE / long-lived HTTP from `/trace` | Bigger UI rewrite (JS ANSI parser, reconnect logic), proxy timeout tuning (nginx `proxy_read_timeout`), and resumption-after-refresh is harder. Polling solves the actual user complaint (slow page, big payloads). | High. Renderer and viewer both rewrite. |
| Cache running-job traces with a short TTL (e.g. 3s) | Doesn't help a single user (they're already the one re-fetching), only helps when ≥2 users watch the same active job simultaneously. Rare in practice, and the cache layout would need to handle "may be stale" semantics. | Low. Add a TTL branch in the repo, keep cache layout. |
| Probe with HEAD + `Content-Length` before re-fetching | GitLab's trace endpoint's HEAD support isn't documented. Even if it worked, the savings are small because every poll still needs the new bytes — the only saving is "definitely nothing new." | Medium. Adds an upstream call to skip an upstream call. |
| Server-side line cache instead of byte cache | Would let us skip re-rendering on the server, not just re-fetching. But the renderer is fast (~ms per MB), the bottleneck is the upstream fetch, and a line cache would invalidate on every running-job poll anyway. | High. Renderer and cache shape both change. |
| Per-job request coalescing (in-process lock or distributed lock) so N concurrent viewers of one job share a single GitLab fetch | Solves only the concurrent case, not the "one user, repeated polls" case. Cross-pod requires Redis distributed lock, which is more complex than the cap approach and still leaves runaway logs as a risk. Caps subsume most of the value (worst case fetch is now ≤ 10 MB). Revisit if metrics show concurrent fan-out is the dominant cost. | Medium. Adds a lock layer in the repo; the cap mechanism stays. |
| Single (hard) cap instead of soft + hard | Loses the early-warning UX. Most users want to see logs that are "big but not enormous" inside the viewer, and a soft warning gives them an escape hatch to GitLab *before* the hard limit kicks them out. The cost of two caps is one extra boolean on the wire and a one-shot banner. | Low. Drop the soft check, remove the banner. |

## Testing

Repository-level (`tests/unit/`):

- Cache miss: full GitLab fetch happens, returned tail equals `full[byte_offset:]`, total_size equals `len(full)`.
- Cache miss with terminal status: cache write is invoked with the right key, status, and payload.
- Cache miss with non-terminal status: cache write is **not** invoked.
- Cache hit: zero GitLab calls, returned tail is sliced from cached bytes.
- Cache write failure: logged + swallowed, repo still returns the data.

Service-level (`tests/unit/test_deploy_service.py`):

- Partial-line hold-back: if upstream returns bytes ending mid-line, `next_byte_offset` rolls back to the last newline and the trailing partial isn't in `lines`.
- Line numbering: `start_line_num` propagates so two consecutive polls produce contiguous `num` values.
- Empty new_text: returns `lines=[]` with `next_byte_offset` unchanged.
- Hard cap: `total_size > GITLAB_TRACE_HARD_CAP_BYTES` returns `too_large=True`, empty `lines`, and does **not** advance `next_byte_offset`. Verified at exactly the boundary and one byte over.
- Soft cap: `total_size > GITLAB_TRACE_SOFT_CAP_BYTES` (but ≤ hard) returns `size_warning=True` with normal lines. The flag rides on every response above the threshold; the viewer dedupes the banner client-side.

Integration: not required — existing trace-route tests cover the basic round-trip; the cache contract is exercised at the unit level.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Redis unavailable → every poll is a cache miss | Cache writes/reads are wrapped to log and degrade gracefully; user still sees their logs, just at the old non-cached cost |
| Running-job trace re-download cost matters in production | Monitor GitLab API rate-limit headroom; if it bites, add the short-TTL running-job cache (see *Alternatives considered*) |
| `MAX_FAILURES` trips on a transient GitLab blip and user has to reload | `5` failures × 5s minimum = ~25s of failure before tripping, which is long enough to ride out blips but short enough not to mask real outages |
| GitLab adds Range-header support later | Repo is the only file that changes — service, renderer, and viewer are insulated by the existing `(status, new_text, total_size)` contract |
| Trace contains a non-UTF-8 byte sequence at the slice boundary | All decodes use `errors="replace"`; never raises |
| A common workload trace size shifts above the hard cap and most users see "log too large" | Caps are env-vars (`GITLAB_TRACE_HARD_CAP_BYTES`) — bump them per-environment without code change. Long-term, watch metrics on `too_large` response rate as a signal that real workloads have outgrown the cap. |
| Hard cap trips during a critical incident, user can't see logs in the viewer | Error panel includes the GitLab link with `project_id` and `job_id`, so the fallback path is one click away. The viewer doesn't leave the user guessing. |
| Soft-cap banner annoys users who routinely view 5–10 MB logs | The banner is one-shot per session (controlled by `sizeWarningShown`) and non-blocking — it sits at the top, log keeps polling. If feedback is bad, raise `GITLAB_TRACE_SOFT_CAP_BYTES` per environment. |
