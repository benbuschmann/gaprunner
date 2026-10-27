# Job recipes

> Words: the web page uses plain words; the config keeps technical names. **Start / Wind down** = `hold = false / true` (a running job with `saved_cmd` stops at its next saved point). **Queue** = `after`, set from the UI; **Queue in order** sets it for several selected jobs at once (each after the one before). **Clear** = `archived` in the queue file (hidden under Cleared, held). **Pause / Resume** = freeze/thaw (SIGSTOP/SIGCONT, nothing lost). **Stop** = end it now (resumes from its checkpoint). **Measure** = calibrate. **Runs: alongside prod / alongside prod, gives way / only while prod sleeps** = `coexist = "share" / "yield" / "no"`; **only during slow-wake prod** = `window_only = true`. **Slow-wake prod** = a window: jobs that are slow to step aside may run while prod sleeps, so a request may wait longer. **No prod** = an exclusive window: prod is stopped for it. **Prod only** = the pause kill switch, **Wind down all** = drain. **Idea** = a planned queue item.

A recipe is one `[[job]]` entry in the config. It tells GapRunner how to start the job, how the job steps aside when a service needs the GPU, where it may run, and how much memory it uses.

```toml
[[job]]
name = "lora-9b"                       # unique; shown in the UI and the queue
kind = "training"                      # free text: training, benchmark, eval, render, ...
goal = "LoRA fine-tune of a 9B model for tool use: 50 steps; done when step 50 is saved and its eval is logged"
notes = "second try, longer context"     # free text, shown in the UI
tags = ["training", "9b"]              # optional; the UI filters and searches by tag (editable there too)
progress_cmd = "tail -1 runs/grpo/metrics.jsonl | jq -r '\"\\(.step)/50\"'"   # prints 12/50 (24% or 0.24 work too)
health_cmd = "curl -sf http://127.0.0.1:18190/health"     # optional; non-zero exit = unhealthy
metrics_url = "http://127.0.0.1:18190/metrics"           # optional; token speed from vLLM/SGLang counters
saved_cmd = "jq -r .step runs/state.json"               # optional; prints the last saved point: Wind down stops there
summary_cmd = "python3 best.py"                         # optional; a few lines shown first in the job's details (e.g. best results so far)
stall_after_s = 5400                   # a step can take 75 min: don't call it stalled before 90

# how to run it (GapRunner starts it; omit start_cmd for a process you start yourself, see "Adopting" below)
workdir = "/srv/jobs/lora-9b"
start_cmd = "python train.py --resume auto"
env = { CUDA_VISIBLE_DEVICES = "0" }
ssd_paths = ["/srv/models/qwen-9b", "/srv/jobs/lora-9b/checkpoints"]   # measured by calibration

# how it steps aside
driver = "restart"                     # see Drivers
park_s = 2.0                           # seeds for the swap costs; replaced by measured times after real swaps
resume_s = 60.0
timeout_s = 120                        # how long a park may take before the job is killed

# where it may run
coexist = "yield"                      # no | share | yield
window_only = false                    # true: only while a window is open
hold = false                           # true: never starts until released (UI or queue file)
yield_resume_s = 5.0                   # yield jobs: resume this long after the services go idle

# memory per tier, GiB: fast GPU memory (HBM), CPU memory, disk
[job.running]
hbm_gib = 30
cpu_gib = 8
ssd_gib = 18

[job.parked]
hbm_gib = 0
cpu_gib = 38                           # e.g. cuda-checkpoint moves the GPU state into CPU memory
ssd_gib = 18
```

`gpu_gib = 30` is shorthand for `running.hbm_gib = 30`. Only `name`, `driver` and the running HBM are required, but every recipe should have a **goal**: what the job is for and what "done" means. `gaprunner check` and the UI point out recipes without one.

## Goal and progress

`goal` is a sentence for the people (and agents) deciding the queue order. `progress_cmd` runs in `workdir` every 30 seconds and prints how far along the job is: `12/50`, `24%` or `0.24`, with any words around it. An optional second line is the part in progress, shown as a second, smaller bar: for a training job, line 1 `6/50 steps` and line 2 `rollouts 18/32` (or `eval 12/40`). GapRunner shows a progress bar and, from the progress per hour of *running* time, an estimate of the GPU-hours left. Calibration samples progress too, so a newly calibrated recipe says roughly how many GPU-hours the whole job needs.

## Wind down at a saved point

`saved_cmd` prints the job's last saved point: a step number, an item count, anything that changes exactly when work
is safely on disk (for a training job, read the file it writes last, its commit point). When you press Wind down on a
running job that has one, GapRunner reads it every 15 s and stops the job as soon as it changes; Start or Queue then
continues from that point. Without `saved_cmd`, Wind down lets the job finish its run and then keeps it off.

## Token speed

A job that runs its own inference server can set `metrics_url` to the server's Prometheus endpoint. GapRunner reads
`vllm:generation_tokens_total` and `vllm:prompt_tokens_total` (or the `sglang:` ones) every 10 s while the job runs and
shows tokens per second out and in, averaged over about 30 s. Services with the `vllm` or `sglang` driver get this
from `<upstream>/metrics` without any setting. It is the server's own count, so prod's speed includes job traffic.

## Where a job runs (`coexist`)

| value | meaning | use it for |
|---|---|---|
| `no` (default) | the job needs memory a service holds: the service parks, the job runs in the gap, and it parks when the next request arrives | big jobs |
| `share` | the job runs beside the hot services whenever it fits; both share the GPU's compute | small jobs where a little service slowdown is fine |
| `yield` | like `share`, but the job pauses its compute (SIGSTOP) while a service has requests in flight, so the service keeps full speed | small jobs beside a latency-sensitive service |

A job whose park time plus the slowest service wake exceeds `policy.max_added_wait_s` runs only in windows, whatever `coexist` says. When a request wakes a service, jobs get `policy.wake_deadline_s` (default 3 x `max_added_wait_s`) to step aside; a job still stepping aside then is stopped hard (`kill_stuck_jobs`), unless its own measured step-aside time, doubled, is longer. In a window, a request may wait as long as the window's jobs take to step aside.

## Drivers (how a job steps aside)

| driver | park | resume | the job must |
|---|---|---|---|
| `restart` | SIGTERM to the job (its whole process group if GapRunner started it), SIGKILL after `timeout_s` | run `start_cmd` again | resume from its own checkpoints (`--resume`): item-resume benchmarks, step-save training |
| `cuda-checkpoint` | NVIDIA cuda-checkpoint: lock, then checkpoint (GPU memory to CPU memory) | restore, then unlock | be a CUDA process with no IPC/UVM sharing; `pgrep`/`pid_file` must name the CUDA process |
| `vllm` | `POST /sleep?level=1` | `POST /wake_up` | be a vLLM server with `--enable-sleep-mode`, `VLLM_SERVER_DEV_MODE=1`; set `url` |
| `sglang` | `POST /release_memory_occupation` | `POST /resume_memory_occupation` | be an SGLang server with `--enable-memory-saver`; set `url` |
| `park-protocol` | `POST /berth/park` | `POST /berth/resume` | speak Berth's park protocol; set `url` |
| `command` | `park_cmd` | `resume_cmd` | provide the hooks; `state_cmd` prints `hot` or `parked` |

## Is it running, or broken?

While a job runs, GapRunner checks every 10 s and flags it **stalled** (report-only, nothing is killed) when:
- its `progress_cmd` value hasn't changed for `stall_after_s` of running time (default 1800);
- its log (`log_file`, or the log GapRunner writes for jobs it starts) hasn't been written for `stall_after_s` while it ran;
- its `health_cmd` (checked every 60 s) exits non-zero;
- it holds more than 1.5× its declared running GPU memory.

A service that should be up but hasn't answered for 30 s is flagged too. A flag clears after 3 clean checks in a row. Raising and clearing go to the audit log and to `notify_cmd` (top-level config), with `GAPRUNNER_LEVEL`, `GAPRUNNER_TARGET` and `GAPRUNNER_MSG` set.

## Sleep if there's room, otherwise stop (`fallback`)

A job that parks by sleeping (`cuda-checkpoint`, `vllm`, `command`) moves its GPU state into host RAM. With `fallback = "restart"` (and a `start_cmd`), GapRunner first checks there's room: if host memory minus the extra the frozen copy needs (`parked.cpu_gib - running.cpu_gib`) would fall below `policy.cpu_floor_gib`, it stops the job instead (SIGTERM to its process group) and runs `start_cmd` again later, so the job resumes from its own checkpoint. Before falling back, GapRunner makes room: it stops jobs that sit frozen or asleep in host memory and are lower in the queue than this one, last in the queue first (each restarts later from its own checkpoint). A job higher in the queue is never stopped to make room for a lower one. Big jobs that can never sleep (their host RAM is already spoken for) simply use `driver = "restart"`.

## Jobs that lose work when they step aside

A `restart` job stepping aside is stopped and started again later from its own checkpoint, so it loses whatever it
did since its last saved point. If saves are far apart (a training step of 30 minutes), short quiet spells cost more
than they give: the job is stopped before it saves, again and again.

GapRunner accounts for that when a recipe has `saved_cmd`:
- It learns the running time between saved points (the first line `saved_cmd` prints changing), checked once a minute
  while the job runs. `save_every_s` is the first guess until then.
- Such a job starts in a quiet spell only when the spell is at least `policy.lossy_p_min` likely (default 50%) to last
  for its come-back time plus that interval. Otherwise it waits, and the page says why ("needs about 40 min of quiet to
  reach its next saved point"). A slow-wake or no-prod period lets it run regardless.
- A job that fits beside the awake service never steps aside for it, so it starts as usual.
- Each step-aside records a `lost_work` event with the running time thrown away.

`loses_work` is `true` for `restart` jobs and `false` for drivers that keep their place (sleep, freeze). Set it for a
`command` job whose hooks stop the job. The better fix, where the job allows it, is a driver that keeps its place, so
it can use every quiet spell.

## Engines: one service, several models

```toml
[[service]]
name = "prod"
listen = "127.0.0.1:18950"            # one gate; your router's alias points here
default_engine = "glm"
schedule = [{ at = "23:00", engine = "qwen27b" }, { at = "07:00", engine = "glm" }]   # optional, local time

[[service.engines]]
name = "glm"
driver = "vllm"
upstream = "http://127.0.0.1:18000"
start_cmd = "docker compose -f /srv/llm/compose.yaml up -d vllm"
stop_cmd = "docker compose -f /srv/llm/compose.yaml stop vllm"
model_name = "glm-5.3-flash"           # the gate rewrites the request's "model" to this
running = { hbm_gib = 210, cpu_gib = 323 }

[[service.engines]]
name = "qwen27b"
# ...
```

Each engine is a recipe: how to start and stop it, where it listens, its memory, its sleep and wake times. Switch with `gaprunner switch <config> prod qwen27b`, the UI, or the schedule. On a switch, the current engine is **stopped** (its GPU and CPU memory are freed), jobs that don't fit beside the new engine are parked, and the new engine is started and waited for (`ready_timeout_s`, default 1800). If it doesn't come up, the old engine is started again. An engine may set `switch_out = "sleep"` to sleep instead of stopping, when switching back fast matters more than the memory.

Jobs that test a service: set `target_service = "prod"` (and optionally `requires_engine = "qwen27b"`). GapRunner starts them with `GAPRUNNER_URL` (the service through the gate, marked as job traffic), `GAPRUNNER_SERVICE` and `GAPRUNNER_ENGINE`. Job traffic keeps the service awake, but isn't counted as the service's own activity (its idle gaps and the pauses of yield jobs). A job that requires an engine runs only while that engine serves, and parks when it stops serving.

## Service fields for host memory and safety

```toml
[[service]]
# ...
release_cmd = "curl -s -X POST http://127.0.0.1:18000/collective_rpc -H 'Content-Type: application/json' -d '{\"method\": \"release_host_cache\"}'"
bypass_cmd = "..."     # run by `gaprunner watchdog` if the gate stops answering (e.g. point the router at the engine)
restore_cmd = "..."    # optional; with auto_restore = true it runs once the gate has answered for 60 s
stop_cmd = "..."       # with start_cmd: used by windows that stop the services
start_cmd = "..."
```

`release_cmd` runs at most once per wake, and only while a job is waiting for host memory, so a service that keeps a large cache (vLLM's pinned sleep buffer) gives it back only when someone needs it. The next park after a release is slower, because the cache is set up again.

## Lifecycle of a recipe job

1. **Waiting**: parked, not held, in queue order. The UI says in words why it isn't running.
2. **Start**: GapRunner runs `start_cmd` in `workdir`, in a new process group, with output to `<state_dir>/job-<name>.log`.
3. **Running**: yield jobs are paused while services are busy. A service wake parks the job if it doesn't fit beside the service.
4. **Parked**: a `restart` job is stopped. GapRunner starts it again (same `start_cmd`) in a later gap, and it picks up from its checkpoint.
5. **Done / failed**: a small wrapper writes the exit code to `<state_dir>/job-<name>.result.json`, even if GapRunner is down at that moment. Exit 0 is **done**, anything else is **failed**. Neither is restarted automatically. To run it again, press **Start** (or **Queue**) on the page: that clears the recorded exit and lets it start.

## Apps that ask before they start (`driver = "governor"`)

Some apps start their own GPU server when a person asks for work, and stop it when they're done. A video studio's
renderer is a typical one. GapRunner can't start these from a recipe, but it can decide when they may start: the app
asks for a reservation, waits in GapRunner's queue in the job's place, and starts its server once GapRunner has made
room. When GapRunner needs the GPU back it revokes the reservation and the app's server is stopped.

The protocol is the workload-governor one Imagination Studio uses (`SPARK_GOVERNOR`). GapRunner writes the folder the
app reads (`governor_dir`): three small client modules, `state/reservations.json` and the socket `state/admission.sock`.

```toml
[gaprunner]
mode = "live"
governor_dir = "/var/tmp/gaprunner-governor"   # the app's user must be able to read it
governor_uids = [1006]                          # the app runs as another user (Linux)

[[job]]
name = "Video renders"
driver = "governor"
owner = "tensorfold-studio-comfy-8190"          # the name the app asks under
order = 0                                       # top of the queue: renders go first, training waits
gpu_gib = 75                                    # the largest render it is allowed to take
comfy_url = "http://127.0.0.1:8190"             # optional: record each render, and release the GPU when idle
idle_release_s = 600                            # with comfy_url: revoke after this long with nothing to render
admit_wait_s = 300                              # a request not admitted by then is refused (the app can render elsewhere)
grace_s = 30                                    # after a revoke: SIGTERM, then SIGKILL after this long
```

On the app's side, set `SPARK_GOVERNOR=/var/tmp/gaprunner-governor` in the environment its renderer starts in.

On the page the job is **idle** while nothing has asked, **waiting** in its place once the app asks, and **running**
while the reservation is held. Wind down keeps it from being admitted: a request then waits `admit_wait_s` and is
refused. Stepping aside loses the render in progress, so the job counts as losing work. A job lower in the queue never
pushes it aside, and max active jobs counts it like any other job.

**What is recorded.** With `comfy_url`, each prompt the app runs adds a row to `<state_dir>/governor-<job>.jsonl`:
its sizes, lengths and steps (`width`, `height`, `length`, `steps`, ...), the model files it loaded, how long it took,
its status, and the GPU and host memory the app's processes held at the peak. A `ready` row records how long the app
took from the grant to its server's first answer. You need these numbers before letting such an app use short quiet
spells: how much memory each kind of work takes, and how long it runs.

**In shadow mode** a request is admitted at once, and the work is still recorded.

**Who may ask.** GapRunner's own user, plus the uids in `governor_uids` (checked with `SO_PEERCRED`). The folder must
belong to GapRunner's user; GapRunner refuses one that doesn't.

## Adopting a job you start yourself

Leave out `start_cmd` and tell GapRunner how to find the job's processes with `pid_file`, `pids`, `pgrep` (a regex over command lines) or `pids_cmd` (a shell command that prints pids, e.g. `docker top <container> -eo pid | tail -n +2` for a container). GapRunner can still pause, park and resume it with the driver you choose, but it can't see its exit code, so such a job is never marked done or failed.

## Calibration: let GapRunner measure the numbers

Write the recipe with what you know (how to start it, how it steps aside, a rough `hbm_gib`) and `hold = true`. Then, with GapRunner in live mode:

```bash
gaprunner calibrate config.toml "lora-9b"        # or Measure in the job's details in the UI
```

GapRunner starts the job (even though it's held) once it fits on the GPU, and samples its GPU memory (per process, from the driver) and CPU memory every second while it warms up. Then it parks the job, timing the park and measuring what it still holds, and resumes it, timing until its GPU memory is back to 90%. It does this twice, then leaves the job parked. The result is in `<state_dir>/calibration/<job>.json` and in the UI, as a ready-to-paste block:

```toml
park_s = 1.8
resume_s = 41.2

[job.running]
hbm_gib = 22.6        # peak 24.1
cpu_gib = 27.9        # peak 31.0
ssd_gib = 19.4

[job.parked]
hbm_gib = 0.0
cpu_gib = 0.0
ssd_gib = 19.4
```

List `ssd_paths` in the recipe (weights, checkpoints, outputs) to have disk use measured. The measured swap times go into the cost table straight away. Calibration never parks a service: if the job doesn't fit beside them, calibrate it in a window. If a service request needs the GPU mid-run, calibration stops and keeps what it had measured.

## Checklist for a new recipe

- [ ] The job resumes from its own state (checkpoint, item log), or uses `cuda-checkpoint`.
- [ ] Calibrated (`gaprunner calibrate`), and the measured block pasted into the recipe.
- [ ] `coexist` is chosen: does it fit beside the service at its peak?
- [ ] `timeout_s` is comfortably above the measured `park_s`.
- [ ] `gaprunner check config.toml` passes and shows the job.
- [ ] Release it from the UI (or set `hold = false`) only after a shadow-mode run shows sensible decisions.
