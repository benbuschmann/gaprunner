# Changelog

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
