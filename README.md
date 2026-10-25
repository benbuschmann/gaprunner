# GapRunner

**Resumable background GPU jobs that yield to your local AI services.**

You run an AI service on a big GPU box (agents hitting a local LLM all day). It's busy in bursts, so between requests the GPU sits idle, sometimes for seconds and sometimes for hours. GapRunner gives those gaps to background work: benchmarks, LoRA/RL training, renders and evals. When the service gets a request, it takes the GPU back within a few seconds:

- **Services always win.** An idle service is *parked* (e.g. vLLM sleep mode: weights move to CPU memory). The next request is held by GapRunner's gate while background jobs are frozen and the service wakes, then it's answered normally.
- **Jobs are frozen, not killed.** The job driver you pick decides how a job steps aside: `cuda-checkpoint` (freeze in place, bit-identical), a step-boundary save, or item-level resume.
- **Gap-aware, not just priority.** Every swap has a measured cost. A job starts only when the current idle gap is *likely* to last long enough to be worth it, learned from your service's own history: P(stays idle N s more | idle for t s so far). Fast-swap jobs take short gaps, slow ones wait for long gaps or scheduled windows.
- **Small jobs don't wait for gaps.** A job that fits in GPU memory beside the hot services runs there (`coexist = "share"`), optionally pausing its compute whenever a service is busy (`coexist = "yield"`).
- **Jobs are recipes.** Each job says how to start it, how it steps aside, and how much fast GPU, CPU and disk memory it uses while running and while parked. GapRunner starts it, tracks it, and records whether it finished or failed.
- **Humans steer from a web UI.** A live picture of who holds the GPU and why, memory over time, the queue (reorder, hold, window-only), windows, swap costs, the gap model and the event log. Changes need a PIN.

Status: **early (0.x); running in production on one workstation since 2026-10-23.** Built for and measured on an NVIDIA DGX Station GB300 (252 GB HBM, 494 GB CPU memory over NVLink-C2C). It should work on any Linux box with one big NVIDIA GPU. Python 3.11+, standard library only.

## How it compares

Model-swapping brokers already exist and are good. [Berth](https://github.com/T-Crypt/berth) (leases, RAM parking, a park protocol), [gpu-broker](https://github.com/emergenthq-net/gpu-broker), [llama-swap](https://github.com/mostlygeek/llama-swap), [llmux](https://github.com/doublewordai/llmux) and [gpuq](https://github.com/elektricM/gpuq) take turns between *services*. Research systems (LLMStation, SIRIUS, ConServe, DeltaServe, PipeSwitch) share a GPU between serving and training inside one engine.

GapRunner covers what those leave out: **freezing and resuming arbitrary jobs into a service's idle gaps, scheduled by measured swap cost against the gap distribution.** It speaks Berth's [park protocol](https://github.com/T-Crypt/berth/blob/main/contrib/PARK-PROTOCOL.md) in both directions. GapRunner can park anything that implements it, and `gaprunner adapter` lets Berth park GapRunner's jobs. See [docs/PRIOR-ART.md](docs/PRIOR-ART.md).

## Quick start

You need a Linux box with an NVIDIA GPU (`nvidia-smi` working) and a model server that can sleep. For vLLM:
`VLLM_SERVER_DEV_MODE=1 vllm serve <model> --enable-sleep-mode --port 8000`.

```bash
pip install gaprunner                       # or from a checkout: pip install -e .
gaprunner example > gaprunner.toml          # a minimal config: one service, no jobs, shadow mode; edit it
mkdir -p ~/.config/gaprunner && (umask 077; echo 'pick-a-pin' > ~/.config/gaprunner/pin)   # for changes from the page
gaprunner check gaprunner.toml --strict     # reads the GPU and every server once, writes nothing; says what isn't ready
gaprunner run gaprunner.toml                # shadow mode: learns your service's quiet spells, logs what it WOULD do
```

Then:

1. Send your clients' requests to the gate (`listen`, here `127.0.0.1:8100`) instead of the server. Or point a router such as llama-swap at it.
2. Open the page at `http://127.0.0.1:8096`. It shows who holds the GPU, prod's state and the queue. Changes need the PIN.
3. After a few days, compare what it would have done (History, `audit.jsonl`) with a replay of your real traffic: `gaprunner replay gaprunner.toml ~/.local/state/gaprunner/activity.jsonl`. Then set `mode = "live"`.
4. Add jobs: [docs/RECIPES.md](docs/RECIPES.md). [examples/gb300-workstation.toml](examples/gb300-workstation.toml) is a fuller config with a benchmark and a LoRA run.

Run it as a service with [examples/gaprunner.service](examples/gaprunner.service). Keep `KillMode=process`, so restarting or upgrading GapRunner leaves running jobs alone. While the gate sits in your clients' request path, also run `gaprunner watchdog gaprunner.toml`, and give the service a `bypass_cmd` that points clients straight at the server if the gate stops answering ([CONFIG.md](docs/CONFIG.md#when-the-gate-stops-answering)).

Kill switch: `gaprunner pause my.toml` wakes every service, parks every job and stops scheduling. `gaprunner unpause my.toml` turns scheduling back on.

## Docs

- [Job recipes](docs/RECIPES.md): placements, drivers, lifecycle, with examples
- [Config reference](docs/CONFIG.md): every field and its default
- [Agent guide](docs/AGENT-GUIDE.md): for AI agents on a GapRunner machine; plus a drop-in [skill](skills/gaprunner/SKILL.md) and [llms.txt](llms.txt)
- [Design](docs/DESIGN.md) · [Prior art](docs/PRIOR-ART.md) · [AGENTS.md](AGENTS.md) for contributors

## Drivers

| driver | for | park / resume |
|---|---|---|
| `vllm` | services, vLLM jobs | `/sleep?level=1`, `/wake_up` (`--enable-sleep-mode`, `VLLM_SERVER_DEV_MODE=1`) |
| `sglang` | SGLang (experimental) | `/release_memory_occupation`, `/resume_memory_occupation` (`--enable-memory-saver`) |
| `park-protocol` | anything speaking Berth's protocol | `POST /berth/park`, `POST /berth/resume` |
| `cuda-checkpoint` | any CUDA process (training, renders) | explicit lock → checkpoint / restore → unlock; remembers frozen pids |
| `command` | anything | your `park_cmd` / `resume_cmd` / `state_cmd` |
| `restart` | step-save, item-resume jobs | SIGTERM; resume = run `start_cmd` again (the job resumes from its checkpoint) |

## Safety rules

- One owner of every transition (a single lock).
- **Memory counts as free only when the driver says so** (nvidia-smi), never on the engine's word.
- Waking a service parks every job first. A job that won't park is killed if `kill_stuck_jobs` is set; if memory still isn't free, the service is woken anyway and the overlap is logged (services first).
- GPU processes that belong to nothing configured are reported as *stowaways*, never killed.
- Anti-thrash (`juggle_s`), a starvation guard (`starvation_s`), and the pause-file kill switch.
- Every transition goes to `audit.jsonl` with its measured time. Costs are re-measured on every real swap.

## What the replay says (one GB300 workstation, real traffic, 66 h)

Prod (12 agents on one LLM) was idle 34% of the time, mostly in short gaps (median 12 s).

| policy | benchmark h/day | LoRA h/day | prod wakes/day | switch overhead | prod added wait p50 / max |
|---|---|---|---|---|---|
| default (p ≥ 0.75, 3× worth, park after 30 s) | 3.7 | 1.2 | 228 | 4.6% | 1.3 s / 4.7 s |
| park after 10 s, p ≥ 0.5 | 5.4 | 4.7 | 601 | 9.8% | 4.7 s / 4.7 s |
| start in every gap > 5 s (baseline) | 6.0 | 5.8 | 787 | 13.1% | 4.7 s / 4.7 s |

The policy knob trades prod latency and wear against GPU hours. The replay learns the gap model as it goes (out-of-sample), so these numbers aren't fitted to the trace they're scored on.

## Security

GapRunner is built for a machine you control, on loopback or a private network. Read [SECURITY.md](SECURITY.md) before exposing anything. In short:

- The config is code: recipes, hooks and notify commands run as shell commands with GapRunner's permissions.
- The PIN guards changes only. Anyone who can reach the page or the gate can read status and job logs.
- The gate passes any path through to your model server.

## License

Apache-2.0. `cuda-checkpoint` (NVIDIA EULA) and CRIU (GPL) are called as external programs and are not included.
