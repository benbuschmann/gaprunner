# Changelog

## 0.28.0

- `progress_every_s`: how often a job's `progress_cmd` is read (30 s, as before, unless set; at least 1 s). An app whose
  work comes in short pieces, such as a video app rendering one clip at a time through the governor, can report each
  clip's progress and have it show while the clip runs.

## 0.27.2

- Fixes two regressions in 0.27.1, both found in review: after a job's process group had fully exited, the recipe's
  own process discovery (`pgrep`, `pids_cmd`) ran again and could attribute an unrelated process to the finished job;
  and a stored submission rebuilt from a template whose `gpu_gib` had grown kept its old, smaller figure. A rebuilt
  job now takes at least the template's current `gpu_gib`, and one over the current limits is dropped.

## 0.27.1

- Jobs API: stored submissions are made again from the current template at each start, never run as stored, so a
  job saved under an older or corrected template can't bring its old commands back. One whose template is gone or
  whose values no longer pass is dropped.
- Governor jobs: a grant revoked before the app bound its command stays counted for as long as its launcher stays
  connected (before, only for `grace_s` + `bind_timeout_s`). A service wake now also takes jobs that are still
  stepping aside, so its deadline and the hard stop apply to them.
- A job's processes are still found, stopped and counted after its wrapper exited and left children running.

## 0.27.0

- **Jobs API templates: values reach commands only as environment variables.** A `{{param}}` slot in a command is
  now refused; commands use `"$GAPRUNNER_PARAM_<NAME>"`, which a shell never runs. Slots stay in `goal`, `notes` and
  `env`. Two reviews kept finding shell constructs that turned a pasted, quoted value back into code; this removes
  the pasting. Templates written for 0.26 must change their commands.
- A job's `env` now reaches every one of its commands (progress, saved point, summary, health, command-driver hooks),
  not only `start_cmd`.
- Governor jobs: a grant revoked before the app bound its command stays counted (stepping aside) until the launcher
  disconnects or its grace runs out, and a bind that arrives after the revoke is recorded and revoked again. The
  launcher reads GapRunner's messages without a buffer that could hold back a revoke, and rebinds when GapRunner goes
  away just as it reports its binding.
- Stopping or withdrawing a job SIGKILLs every process it had, including children left behind after their group
  leader exited.

## 0.26.3

More fixes from a second, independent review of the Jobs API and governor jobs:

- A template slot right after a backslash (`\\{{x}}`), glued to `$`, or in a command given by path (`/bin/sh -c`) is
  refused. The backslash case let a value run commands.
- `NaN` and `Infinity` are refused in Jobs API bodies, and `gpu_gib` must be finite. A `NaN` passed every limit, was
  stored, and made `/api/status` unreadable for the page.
- Withdrawing a job holds the scheduler's lock throughout, so a job that starts at that moment is stopped, not lost.
- Governor jobs: a grant no longer waits for the app under the scheduler's lock (an admitted app could hold up a
  service's wake for up to 120 s); an app that doesn't bind its command within `bind_timeout_s` (30) is revoked. The
  token is saved before the app hears it, and a launcher whose reservation a restarted GapRunner doesn't know stops
  its command. The command gets SIGTERM from the kernel if its launcher dies, so it never outlives the one process
  GapRunner can reach it through. A prompt cut off by a revoke is recorded as `interrupted`.

## 0.26.2

Security fixes for the Jobs API and governor jobs, from an end-to-end test and a review:

- A template whose `{{slot}}` sits inside quotes, or in a command that reads its arguments as code again (a
  here-document, `eval`, `sh -c`, `python -c`, `ssh`, `xargs`, ...), is refused: a value could run commands there.
  Use `$GAPRUNNER_PARAM_<NAME>` in such commands.
- Submitted jobs are named `<client> <id>: <label>`, and a job whose files would share a name with another job's is
  refused. Before, two long labels could share pid and result files, so one client could stop another's job.
- A request with a negative or non-numeric `Content-Length` is answered 400 (it could make GapRunner read without
  end), and a client that stops sending mid-request is dropped after 60 s. Malformed Jobs API bodies get 400.
- A caller can't ask for less GPU memory than the template's `gpu_gib`, nor start a job the template or client holds.
- Client names are 1 to 24 letters, digits, `-` or `_`. Removing a client revokes its stored jobs at the next start.
- Governor jobs: a binding must be the asking user's own process session, started by its launcher; only that user can
  release the reservation; after a restart only a token GapRunner issued can be taken back. The governor folder may
  not be a symbolic link or sit in a folder others can write without the sticky bit. Reservations are written before
  the app is told it may start.

## 0.26.1

- A service started at the end of a window (or restarted after a failed wake) is no longer put to sleep for being idle
  before GapRunner has seen it ready. Seen on a GB300 twice in one day: prod came up with no requests, was parked in
  the same tick it first answered and never marked ready, so the gate answered 503 "starting again" to every request
  until GapRunner was restarted or `ready_timeout_s` ran out.

## 0.26.0

- Jobs API: other programs put jobs into the queue. The operator writes `[[template]]` entries (job recipes with
  `{{param}}` slots and typed, limited parameters) and gives each program a token (`[[client]]`, holding only the
  token's SHA-256) that names the templates it may use, a GPU memory cap, how many unfinished jobs it may have and
  where its jobs enter the queue. `POST /api/jobs` fills a template in (values shell-quoted in commands, and exported
  as `GAPRUNNER_PARAM_<NAME>`) and queues the job; `GET /api/jobs`, `GET /api/jobs/<id>` and `DELETE /api/jobs/<id>`
  follow it and withdraw it; `GET /api/templates` lists what a token may use. Submitted jobs survive a restart.
  `gaprunner token` makes a token and its hash. See docs/JOBS-API.md.
- A governor job's request that can't be admitted soon (wound down, turned off, window-only with no window open, the
  kill switch or Wind down all on) is turned down at once with the reason, instead of waiting `admit_wait_s`.
  `/api/status` shows `admits_now` and `not_now` for each governor job; `examples/wass_worker_sync.py` uses them to
  keep a machine in Imagination Studio's worker list only while GapRunner would admit its renderer.

## 0.25.0

- Jobs an app asks for (`driver = "governor"`). An app that starts its own GPU server when someone asks for work
  asks GapRunner for a reservation and waits in the queue in the job's place; GapRunner admits it like any job and
  revokes the reservation when it needs the GPU back (the app's server gets SIGTERM, then SIGKILL after `grace_s`).
  The protocol is the workload-governor one Imagination Studio uses (`SPARK_GOVERNOR`): GapRunner writes the folder
  the app reads (`governor_dir`) and serves its admission socket, open to the users in `governor_uids`. A request not
  admitted within `admit_wait_s` is refused, so the app can render elsewhere. A job nothing has asked for shows as
  **idle** and holds no place on the GPU. In shadow mode requests are admitted at once.
- With `comfy_url`, each prompt such an app runs is recorded in `governor-<job>.jsonl` (sizes, lengths, steps, the
  model files, time, status, GPU and host memory at the peak), and the reservation is revoked after `idle_release_s`
  with nothing to run.

## 0.24.1

- The page shows Stop for a job that stepped aside by freezing or sleeping and still holds memory. Before, Stop
  appeared only for running or hand-paused jobs, so such a job could not be stopped from the page to free its memory.
- A park that stopped a job (or otherwise kept nothing in host memory) is no longer recorded as a host-memory sample.
  Those zeros pulled the measured figure down for jobs that sometimes freeze and sometimes stop.

## 0.24.0

- A service isn't put to sleep while its `warm_cmd` is still running after a start or a wake (at most
  `warm_hold_max_s`, default 300 s). Seen on a GB300: a service put to sleep 14 s after a cold start, in the middle of
  its warm-up, crashed its engine.
- A service that fails to wake no longer makes every request wait out another wake. Requests get a 503 at once that
  says why, and when the service has `stop_cmd` and `start_cmd`, GapRunner restarts it (a `service_restart` event),
  the way the end of a no-service period starts it again. Without them, a request may try a wake again after
  `dead_retry_s` (default 60 s). Set `restart_on_failed_wake = false` to keep the old behaviour. Before, each request
  to a crashed engine waited the full wake timeout, and those waits held the lock that stopping the service needed.

## 0.23.0

- Host memory a park takes is measured. GapRunner reads MemAvailable before and after each park of a service or job
  and keeps the median of recent parks (the `park` event shows `host_gib`). Decisions use the larger of that and the
  recipe's `parked.cpu_gib - running.cpu_gib`, so a recipe that understates it can no longer let host memory run out.
  Seen on a GB300: a service declared at 41 GiB actually took about 170 GiB of host memory to sleep, which with a job
  frozen in host memory would have run the machine out of memory. The job page shows the measured figure beside the
  recipe's.
- Each run's GPU peak is measured and shown on the job page beside the recipe's `running.hbm_gib`, marked when the
  recipe understates it by more than 10%. Scheduling still uses the recipe's figure: some engines take whatever GPU
  memory is free, so a measured peak isn't always what the job needs.
- A job paused by hand no longer holds a max active jobs slot, as in qBittorrent, where a paused torrent doesn't count
  toward the limit. Its GPU memory is still counted, so another job starts beside it only if it fits. When it is
  resumed with the slots full, the active job lowest in the queue steps aside.

## 0.22.0

- Host memory follows queue order. When a job needs to step aside by freezing or sleeping and its copy doesn't fit in
  host memory, GapRunner first stops jobs that sit frozen or asleep in host memory and are lower in the queue, last
  first, until there is room (an `evicted` event each). They start again later from their own checkpoints. Only if
  that isn't enough does the job itself fall back to stopping. A job higher in the queue is never stopped for a lower
  one.

## 0.21.0

- Max active jobs follows queue order for running jobs too. When the limit is full and a job higher in the queue could
  start, the active job lowest in the queue steps aside to make room ("makes room for …, higher in the queue"), as in
  qBittorrent. Before, the higher job waited until the lower one happened to step aside, which in a quiet spell could
  take hours. A job that loses work when it steps aside (`loses_work`) is never pushed aside; it keeps its slot until
  it steps aside or finishes. Only queue position counts: the starvation guard never pushes a running job aside.

## 0.20.1

- Max active jobs: when a job steps aside it waits `juggle_s` (15 s) before it may come back, and in that window a
  job lower in the queue could take the free slot and keep it until the next step-aside, so the two took turns instead
  of following queue order. The slot is now kept for the job that stepped aside; the other job says so while it waits.

## 0.20.0

- Max active jobs: `policy.max_active_jobs` caps how many jobs are on the GPU at once (0, the default, means no limit),
  like qBittorrent's "Max active torrents". The rest wait their turn in queue order and say so ("max active jobs
  reached (1 of 1 running)"). A service isn't put to sleep for a job that has no slot. Lowering the limit steps aside
  the jobs last in the queue.

## 0.19.0

- Jobs that lose work when they step aside. A `restart` job loses what it did since its last saved point, so a job with
  long steps (30-minute training steps) could be stopped before every save in a run of short quiet spells. GapRunner now
  learns each such job's time between saved points from `saved_cmd` and starts it only in quiet spells at least
  `policy.lossy_p_min` (50%) likely to last until its next save. Jobs that fit beside the awake service, jobs that sleep
  or freeze, and slow-wake / no-prod periods are unaffected. New recipe fields `loses_work` (default: true for
  `restart`) and `save_every_s` (first guess); each step-aside records a `lost_work` event; the job's details show its
  save interval and unsaved time.

## 0.18.3

- Page: a job never gets a service's colour. Services take the first colours and jobs cycle through the rest (before,
  the ninth job wrapped round to the service's blue).
- Recipes can name a job's colour: `color = "orange"` (blue, orange, teal, purple, olive, pink, indigo or amber, the
  page's palette, checked for colour-blind contrast in light and dark mode).

## 0.18.2

- vLLM driver: when `/is_sleeping` times out (a server busy with slow model steps), the driver keeps its last real
  answer for up to 2 minutes instead of reporting the state as unknown. An unknown state sent requests down the wake
  path for a server that was already awake: the wake then waited for GPU memory the server itself held, until the
  wake deadline (seen as ~20 s waits and `overlap_risk` events). A refused connection or an error status is still
  unknown at once.

## 0.18.1 (first public release)

Everything below, published together.

- A job stopped by its own park_cmd (the wrapper outlives it and records the exit code) is no longer marked
  failed: the record is dropped and the job starts again later, as with any park.

## 0.18.0

- `warm_cmd` for service engines: run in the background after a wake or a start, for engines that are slower until
  something is read back into memory. Requests don't wait for it; the page shows "warming up", and the history shows
  how long it took.
- A job started during a no-service period says so in its start reason (it said "slow-wake").

## 0.17.0

GapRunner runs resumable GPU jobs in the idle gaps of an always-on model server on one NVIDIA GPU, and gives the GPU
back when a request arrives.

- **The gate and the scheduler:** a gate in front of each service; jobs start in quiet spells that are likely to last,
  judged from measured step-aside and come-back times against the gaps seen so far; shadow mode and `replay`.
- **Drivers:** vLLM and SGLang sleep/wake, cuda-checkpoint freeze, restart-from-checkpoint, shell commands, and
  Berth's park protocol (as a client, and as a server with `gaprunner adapter`).
- **Placements:** jobs that run beside a service, beside it giving way while it answers, only while it sleeps, or only
  during slow-wake / no-service periods. Engine switching for a service, by hand or on a schedule.
- **Safety:** a deadline on request wakes (a stuck job is stopped hard), memory counted free only when the driver says
  so, CPU and GPU memory floors, a kill switch, health checks with alerts, a watchdog that routes around the gate.
- **The web page:** the queue as a table with search, tags, bulk actions and drag to reorder; a details pane with the
  log, recipe, history and a job's highlights; a timeline of what's coming up; live updates; engine stats (tokens/s,
  time to first token, KV cache, prefix hits); memory per job and per tier, with unknown use named where it can be.
- **Recipes:** goals, progress, saved points (Wind down stops at the next one), summaries, measured footprints
  (`gaprunner calibrate`), claims for work started by hand.
- `gaprunner example`, `gaprunner check --strict`, a config reference, an agent guide, a drop-in skill and llms.txt.

Earlier versions (0.1 to 0.16) were developed privately on one workstation; their history isn't published.
