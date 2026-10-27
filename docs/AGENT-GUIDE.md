# GapRunner for AI agents

This guide is for AI agents (coding assistants, ops agents, research agents) working on a machine where GapRunner shares the GPU. It covers what you may and may not do, how to read state, and how to get your work onto the GPU.

## The rules

1. **Never kill, stop or restart a GPU process you didn't start**, and never `nvidia-smi --gpu-reset`. GapRunner and the services it protects own the GPU. If memory looks taken, read `/api/status`: the holder is named there, along with why it's there.
2. **Never start GPU work outside GapRunner on a machine it manages.** Your process would show up as "not tracked", break GapRunner's memory accounting, and risk running a service out of memory. Write a recipe and queue it instead (below).
3. **Services come first.** Don't change a service's config, port or engine flags. Ask the operator.
4. **Changes need the operator's PIN.** You read freely. Queue edits, pause and windows go through the UI or `POST /api/action` with the PIN, and only if the operator has given you one for that purpose.
5. **The kill switch is for emergencies:** `gaprunner pause <config>`, or `POST /api/action {"action": "pause"}`. Use it only if a service is failing because of a job, and say so to the operator right away.

## Reading state (no PIN needed)

The UI listens on `ui_listen` from the config (for example `http://127.0.0.1:8096`).

| GET | returns |
|---|---|
| `/api/status` | mode (shadow/live), paused, window, GPU reading, memory per holder, untracked processes, every service and job with `state`, `word` and `why` (a sentence), memory per tier, swap costs, policy, queue |
| `/api/history?s=3600` | GPU memory by holder over time (5 s rows for the last hour, 1 min rows beyond) |
| `/api/activity?s=3600` | service request start/end events |
| `/api/events?n=200` | the audit log, newest first: parks, resumes, wakes, starts, exits, kills, UI changes |
| `/api/summary?s=86400` | job GPU-hours, service wakes, added wait p50/p95, failures |

Each gate also answers `GET /gaprunner/state`. The command `gaprunner status <config>` prints the last saved status.

To answer "why isn't my job running?", read `jobs[<name>].why` in `/api/status`. It names the blocker: held, windows only, gap too short (with the probability), not enough memory (with the numbers), or a service still on the GPU.

## Getting work onto the GPU

**If the operator gave you a Jobs API token,** use it: `GET /api/templates` lists what you may run and with which
parameters, and `POST /api/jobs` queues one (see [JOBS-API.md](JOBS-API.md)). You fill in values; the commands are
the operator's. Otherwise, write a recipe for the operator:

1. **Make it resumable.** It should restart from its own checkpoint (`--resume`), or be a plain CUDA process that `cuda-checkpoint` can freeze.
2. **Measure it once.** Note its peak GPU memory, CPU memory and disk use, plus how long it takes to stop and to resume.
3. **Write a recipe** following [RECIPES.md](RECIPES.md) and add it to the config. Start with `hold = true`.
4. **Validate:** `gaprunner check <config>` must pass and list the job.
5. **Ask the operator** to restart GapRunner and press **Start** (or **Queue**) on the job in the page. Until then the job is visible but wound down, and never starts.
6. **Watch it:** `/api/status` and `/api/events`, and its log at `<state_dir>/job-<name>.log`.

For an idea that isn't runnable yet, add a *planned* item to the queue (`{"action": "add", "name": ..., "notes": ...}`, PIN needed) so the operator can see and order it.

## Writing results

A recipe job's exit code is recorded in `<state_dir>/job-<name>.result.json`: 0 is done, anything else is failed. Neither is restarted automatically. Put your real outputs (metrics, checkpoints) in the job's own `workdir`, not in GapRunner's state directory.

## What GapRunner guarantees, and what it doesn't

- **Guaranteed:**
  - Outside windows, only jobs whose measured step-aside time plus the service's wake time fits in `max_added_wait_s` run while a service sleeps, so a request normally waits about that long at most.
  - When a request wakes a service, jobs get `wake_deadline_s` (default 3 x `max_added_wait_s`) to step aside; one that's stuck is stopped hard (`kill_stuck_jobs`), except that a job's own measured step-aside time, doubled, is always allowed. Waits beyond the budget are logged (`request_wake` with `over_budget_s`).
  - In a window ("slow-wake" on the page), slower jobs run too, and a request may wait as long as they take to step aside.
  - GPU memory is counted as free only when the driver says so.
  - Every transition is in the audit log.
- **Not guaranteed:**
  - That your job gets any particular share of the GPU. It runs when gaps or memory allow, in queue order.
  - That a `share` job doesn't slow a service. Use `yield` if that matters.
