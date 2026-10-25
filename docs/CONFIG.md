# Config reference

One TOML file: `[gaprunner]`, `[policy]`, one or more `[[service]]`, any number of `[[job]]` and `[[claim]]`.
`gaprunner example` prints a minimal one; [RECIPES.md](RECIPES.md) explains jobs with examples. Paths may start
with `~`. Every command field runs through `sh -c` with GapRunner's user and permissions: the config is code (see
[SECURITY.md](../SECURITY.md)). Check a config with `gaprunner check <config> --strict`.

## `[gaprunner]`

| key | default | meaning |
|---|---|---|
| `mode` | `"shadow"` | `shadow`: watch, learn gaps, log what it would do. `live`: act. |
| `state_dir` | `~/.local/state/gaprunner` | logs (`audit.jsonl`, `activity.jsonl`, `history.jsonl`), measurements, job logs and results |
| `tick_s` | `1.0` | how often the scheduler runs |
| `gpu_index` | the GPU with the most memory | an index or a `"GPU-..."` UUID |
| `gpu_total_gib` | read from the driver | override the GPU's size |
| `verify_free_s` | `30.0` | how long to wait for the driver to show memory free before starting or waking something |
| `kill_stuck_jobs` | `true` | stop a job hard when it won't step aside in time, so a service can wake |
| `queue_file` | `<state_dir>/queue.json` | job order and the page's changes (Start/Wind down, Queue, tags, ...) |
| `window_file` | `<state_dir>/WINDOW` | the page's slow-wake / no-service period: present while open (JSON `{"until", "exclusive"}`) |
| `windows` | `[]` | daily slow-wake periods, local time, e.g. `["01:00-06:00"]` |
| `pause_file` | `<state_dir>/PAUSE` | the kill switch: while it exists, services stay awake and every job steps aside |
| `ui_listen` | none (no page) | the web page's address; keep it on loopback or a private network |
| `ui_pin_file` | none (read-only) | a file holding the PIN changes need; missing or empty = read-only |
| `notify_cmd` | none | run on health alerts, with `GAPRUNNER_LEVEL`, `GAPRUNNER_TARGET`, `GAPRUNNER_MSG` set |
| `notify_file` | none | and/or append each alert to this file as a JSON line |

## `[policy]`

| key | default | meaning |
|---|---|---|
| `p_min` | `0.75` | start a job in a quiet spell when it's at least this likely to last long enough |
| `worth_factor` | `3.0` | "long enough" = this many times the job's step-aside plus come-back time |
| `max_added_wait_s` | `5.0` | outside slow-wake periods, only jobs that step aside (plus the service's wake) within this run while it sleeps |
| `wake_deadline_s` | `0` = 3 x `max_added_wait_s` | when a request wakes a service, jobs get this long to step aside before a stuck one is stopped hard; a job's own measured step-aside time, doubled, is always allowed |
| `juggle_s` | `15.0` | a job that just stepped aside waits this long before coming back |
| `starvation_s` | `3600.0` | a job waiting this long goes to the front of the queue |
| `reserve_gib` | `4.0` | GPU memory always kept free |
| `cpu_floor_gib` | `16.0` | CPU memory always kept free (no start or sleep below it) |
| `cold_start_after_s` | `120.0` | until 20 quiet spells are learned, jobs wait for this much quiet |
| `park_only_if_work` | `true` | put a service to sleep only when a job can use the time |

## `[[service]]`

A model server GapRunner protects. Required: `name`, `listen`, and either the engine fields below or `[[service.engines]]`.

| key | default | meaning |
|---|---|---|
| `name` | | the service's name, shown on the page |
| `listen` | | the gate's address; clients send requests here |
| `idle_park_s` | `30.0` | put it to sleep after this much quiet (only when a job can use the time) |
| `passive_paths` | `/health`, `/v1/models`, `/metrics`, `/version`, `/is_sleeping`, `/ping` | requests that never wake it or count as use |
| `request_timeout_s` | `1800` | how long the gate waits for the server's answer |
| `bypass_cmd` | none | run by `gaprunner watchdog` when the gate stops answering (see below) |
| `restore_cmd`, `auto_restore` | none, `false` | run when the gate answers again (with `auto_restore`, after 60 s of answers) |
| `default_engine`, `schedule`, `switch_drain_s` | first engine, `[]`, `120` | engines: which serves at start, daily switches (`[{at = "23:00", engine = "b"}]`), how long a switch waits for answers underway |

**Engine fields** (on the service itself, or on each `[[service.engines]]` with its own `name`):

| key | default | meaning |
|---|---|---|
| `driver` | | `vllm`, `sglang`, `park-protocol` or `command` (see RECIPES.md, Drivers) |
| `upstream` | | the server's own address |
| `model_name` | none | the gate rewrites each request's `"model"` to this (one alias, several engines) |
| `gpu_gib` or `[running]`/`[parked]` | | memory awake / asleep, per tier: `hbm_gib`, `cpu_gib`, `ssd_gib` |
| `wake_s`, `park_s` | `1.0`, `1.0` | first guesses for wake and sleep time; replaced by measurements |
| `sleep_level` | `1` | vLLM: 1 keeps weights in CPU memory, 2 drops them |
| `headers` | `{}` | extra HTTP headers for the driver's calls (e.g. an API key) |
| `start_cmd`, `stop_cmd` | none | needed for engine switching and for no-service periods; set both or neither |
| `start_timeout_s`, `stop_timeout_s`, `stop_grace_s`, `ready_timeout_s` | `300`, `300`, `60`, `1800` | limits for those commands, for answers underway before a stop, and for a started engine to answer |
| `switch_out` | `"stop"` | when switched away: `stop` (frees everything) or `sleep` (faster to switch back) |
| `release_cmd` | none | frees the server's cached CPU memory when a job needs it (e.g. vLLM's pinned sleep buffer) |
| `warm_cmd` | none | run in the background after the engine wakes or starts, to bring it back to full speed (e.g. read tables that live in the page cache back into memory); requests are answered meanwhile, and the page shows it as warming up. Output goes to `<state_dir>/service-<name>-warm.log` |
| `pids_cmd`, `pgrep`, `pid_file` | none | how to find its processes (for memory and "unknown use"); `pids_cmd` suits containers |
| `metrics_url` | `<upstream>/metrics` for vllm/sglang | Prometheus metrics for the page's engine stats |

## `[[job]]`

See [RECIPES.md](RECIPES.md) for the walk-through. Required: `name`, `driver`, and `gpu_gib` (or `[job.running] hbm_gib`).

| key | default | meaning |
|---|---|---|
| `driver` | | `restart`, `cuda-checkpoint`, `vllm`, `sglang`, `park-protocol`, `command` |
| `goal`, `notes`, `kind`, `tags` | | what it's for and when it's done; free text; a label; tags for the page |
| `start_cmd`, `workdir`, `env` | none, `.`, `{}` | how GapRunner starts it (in its own process group, output to `<state_dir>/job-<name>.log`) |
| `log_file` | that log | a log GapRunner should show and watch instead |
| `coexist` | `"no"` | `no` (only while services sleep), `share` (beside them), `yield` (beside them, pausing while they answer) |
| `yield_resume_s` | `5.0` | `yield` jobs: carry on this long after the services go quiet |
| `window_only` | `false` | run only during slow-wake / no-service periods |
| `hold` | `false` | start wound down (Start on the page lets it run) |
| `enabled` | `true` | `false` hides it from scheduling |
| `order` | its place in the file | queue position until the page reorders it |
| `after` | `[]` | jobs that must finish (exit 0) first; the page's Queue sets this too |
| `target_service`, `requires_engine` | none | a job that tests a service; runs only while that engine serves; gets `GAPRUNNER_URL` |
| `park_s`, `resume_s`, `timeout_s` | `5.0`, `5.0`, `120` | first guesses for step-aside and come-back time; how long a step-aside may take |
| `fallback` | none | `"restart"`: if CPU memory is too short to sleep, stop it instead (needs `start_cmd`) |
| `max_hours` | none | step aside and wind down after this many hours of running |
| `[running]`, `[parked]` | | memory per tier (`hbm_gib`, `cpu_gib`, `ssd_gib`); `ssd_paths` lets Measure fill in disk use |
| `progress_cmd` | none | prints progress (`12/50`, `24%`, `0.24`); an optional second line is the part in progress |
| `saved_cmd` | none | prints the last saved point; Wind down stops the job when it changes |
| `summary_cmd` | none | prints a few lines shown first in the job's details (e.g. best results so far) |
| `health_cmd`, `health_grace_s` | none, `900` | non-zero exit = unhealthy (after the grace period at start) |
| `stall_after_s` | `1800` | flag it as stuck when neither its progress nor its log has moved for this long |
| `metrics_url` | none | Prometheus metrics of the job's own server, for speed and engine stats |
| driver fields | | `url`, `headers` (vllm, sglang, park-protocol); `park_cmd`, `resume_cmd`, `state_cmd` (command); `pid_file`, `pids`, `pids_cmd`, `pgrep`, `sudo`, `cuda_checkpoint` (cuda-checkpoint and adopted jobs) |

## `[[claim]]`

Work started by hand that GapRunner should know about.

| key | default | meaning |
|---|---|---|
| `name` | | shown as "claim: name" |
| `action` | | `track`: count its memory (not "unknown"); `hold`: start nothing while it runs |
| `pgrep` or `pids_cmd` | | how to find its processes |
| `linger_s` | `15.0` | keep the claim this long after its processes are gone |

## When the gate stops answering

While clients send requests to the gate, GapRunner is in their path. Run `gaprunner watchdog <config>` beside
`gaprunner run` (as its own systemd unit) and give the service a `bypass_cmd` that sends clients straight to the
server. For example, with llama-swap in front:

```toml
[[service]]
# ...
bypass_cmd = "cp /etc/llama-swap/direct.yaml /etc/llama-swap/config.yaml && systemctl reload llama-swap"
restore_cmd = "cp /etc/llama-swap/via-gate.yaml /etc/llama-swap/config.yaml && systemctl reload llama-swap"
auto_restore = false          # put traffic back through the gate by hand once you've looked at why it stopped
```

`direct.yaml` points the model alias at `upstream`; `via-gate.yaml` points it at `listen`. While bypassed, jobs
keep their GPU memory, so wake the service first (`gaprunner pause <config>`) if it was asleep.
