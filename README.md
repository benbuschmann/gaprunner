# GapRunner

**Run training, evals and benchmarks in the idle gaps of your always-on local model, and give the GPU back the moment a request arrives.**

![The GapRunner page: the model server asleep, an eval job holding 76 GiB of GPU memory, and the last hour of the two taking turns](docs/images/gaprunner-now.png)

A local model server sits on the GPU all day. Between requests it is mostly idle, while your training runs, evals
and benchmarks wait for a free GPU. GapRunner hands those idle gaps to the waiting jobs. When a request comes in, the
jobs step aside (they sleep, freeze or stop), the model wakes, and the request is answered as usual.

It is one Python package (standard library only, Apache-2.0) that runs beside your model server on a single NVIDIA
GPU. It is early (0.x) and has been running in production on one NVIDIA DGX Station GB300 since October 2026.

## One night on a GB300 Station

Over **7.3 hours and 1,214 requests** to the model:

| | |
|---|---:|
| Time the model spent asleep, giving the GPU to jobs | **51%** |
| GPU time the background eval job got | **3.7 GPU-hours** |
| Requests that waited nothing extra | **85%** |
| Added wait, averaged over all requests | **1.3 s** |

- **The model:** DeepSeek V4.1 Flash on vLLM with sleep mode. It falls asleep in about 10–12 s and wakes in about
  2.5 s. It is back to full speed about 20 s after a wake, once its `warm_cmd` has read its lookup tables back into
  memory; requests are answered during that time.
- **The job:** a Qwen 27B BF16 evaluation with LoRA adapters. It steps aside in about 1–3 s and comes back in about
  1.5–2 s: its park hook puts its own vLLM server to sleep and freezes what is left with cuda-checkpoint, leaving 0 GiB
  on the GPU.

That's one machine and one workload. Yours will look different.

## How it works

```
clients ──> gate (one per model service) ──> your model server (vLLM, SGLang, ...)
               │ sees every request, holds it while the model wakes
               v
            runner ── every second ──> scheduler: who should hold the GPU now?
               ├── drivers: put services to sleep and wake them; park and resume jobs
               └── nvidia-smi: what is really on the GPU, per process
```

1. **The gate** is a small proxy in front of each model service. Clients send requests to it instead of to the
   server. It sees every request, wakes the model if it is asleep, holds the request until the model is ready, and
   streams the answer back. Health probes (`/health`, `/v1/models`, `/metrics`, ...) pass straight through and never
   wake it.
2. **The scheduler** decides, once a second, who should hold the GPU. It learns how long your service's quiet spells
   last and measures how long each job takes to step aside and come back, then starts work when a gap is worth the
   swap. An idle model is put to sleep only when a waiting job can use the time; otherwise its next request costs
   nothing.
3. **Drivers** do the stepping aside: vLLM or SGLang sleep mode for model servers, cuda-checkpoint to freeze a CUDA
   process in place, a restart from the job's own checkpoint, or your own shell hooks.

The scheduler is a pure function of a snapshot, so the daemon and `gaprunner replay` run identical rules. See the
[design](docs/DESIGN.md).

## Features

| | |
|---|---|
| **Placements** | Each job says where it may run: beside the model (`share`), beside it but pausing its compute while the model answers (`yield`), only while the model sleeps (`no`), or only in slow-wake / no-model periods (`window_only`). |
| **Queue** | Queue order, `after` chains (run B once A has finished), a starvation guard, a host-memory floor and a GPU memory reserve. |
| **Engines** | Several engines behind one service name, switched by hand, from the page or on a daily schedule. The gate rewrites the request's model name, so clients keep one alias. |
| **Recipes** | A job is a TOML entry: how to start it, its goal, progress, saved-point, summary and health commands, its memory per tier, a colour. `gaprunner calibrate` measures the numbers for you; `warm_cmd` brings an engine back to full speed after a wake. |
| **Web page** | One self-contained HTML file (no CDN, no build step) with live updates. See below. |
| **For agents** | An [agent guide](docs/AGENT-GUIDE.md), a drop-in [skill](skills/gaprunner/SKILL.md) and [llms.txt](llms.txt), so coding agents can read state, queue jobs and explain why a job is waiting. |

### Drivers

| Driver | How it steps aside and comes back |
|---|---|
| `vllm` | Sleep and wake through vLLM's API. |
| `sglang` | Release and resume memory occupation (experimental). |
| `cuda-checkpoint` | Move a CUDA process's GPU state to host memory, then restore it. |
| `restart` | Stop the job, then run `start_cmd` again; the job resumes from its own checkpoint. |
| `command` | Your own park, resume and state hooks. |
| `park-protocol` | Berth's park protocol, in both directions: GapRunner parks anything that implements it, and `gaprunner adapter` lets Berth park GapRunner's jobs. |

### The web page

- **Now:** GPU memory by holder, memory over time (1 h to 7 d) with the model's busy periods marked, and a card per
  model service with its state, measured sleep and wake times, and engine stats (tokens/s, time to first token, KV
  cache use, prefix-cache hits).
- **Jobs:** the queue as a table with search, tags, drag to reorder and bulk actions; a details pane with the job's
  log, recipe, run history and its own highlights (from `summary_cmd`); a timeline of what is coming up.
- **History** and **Help** tabs. Every job and service says in a sentence why it is (or isn't) running.
- Engine switching, "Slow-wake" periods (slower jobs may run while the model sleeps; requests may wait longer) and
  "No prod" periods (the model is stopped so a big job gets the whole GPU, then started again).
- Claims for work you started by hand, so its memory is counted or scheduling holds while it runs; GPU memory nobody
  claims is shown as unknown, and model servers GapRunner doesn't manage are named where it can find them.
- Reading needs nothing; every change needs a PIN.

## Keeping the model first

- **A deadline on request wakes.** When a request needs the GPU, jobs get `wake_deadline_s` to step aside (at least
  twice a job's own measured step-aside time); a stuck job is stopped hard and the wake goes ahead.
- **Memory counts as free only when nvidia-smi shows it free.** GPU processes that belong to nothing configured are
  reported and left alone.
- **Prod only:** a kill switch (the button on the page, or `gaprunner pause` / `unpause`): every job steps aside, the
  services stay awake and nothing new starts.
- **Wind down:** a job with a `saved_cmd` stops at its next saved point; without one, it finishes its current run.
- **Health checks** flag stalled jobs and services that stop answering, and send alerts anywhere through
  `notify_cmd` (a phone push, a webhook).
- **A watchdog** (`gaprunner watchdog`) runs your `bypass_cmd` to send clients straight to the model server if the
  gate stops answering.
- **Shadow mode** watches, learns your quiet spells and logs what it would do, without acting. **`replay`** runs a
  real request log through the same rules, so you can preview a policy before it touches the GPU.
- Every transition goes to `audit.jsonl` with its measured time.

## Try it (shadow mode first)

You need **Linux, Python 3.11+ and an NVIDIA GPU** with `nvidia-smi` working, and a model server that can sleep
(macOS runs the test suite only). GapRunner is **not on PyPI yet**; install it from GitHub:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install git+https://github.com/benbuschmann/gaprunner
gaprunner example > gaprunner.toml        # one vLLM service, no jobs, shadow mode: edit its upstream and memory
```

Start your model server with sleep mode, for vLLM:

```bash
VLLM_SERVER_DEV_MODE=1 vllm serve <model> --enable-sleep-mode --port 8000
```

Check the config (it reads the GPU and every server once and writes nothing), then run:

```bash
gaprunner check gaprunner.toml --strict
gaprunner run gaprunner.toml
```

With the example addresses:

1. Point clients at the gate, `http://127.0.0.1:8100` (OpenAI-compatible clients: base URL
   `http://127.0.0.1:8100/v1`), or point a router such as llama-swap at it.
2. Open the page at `http://127.0.0.1:8096`. It is read-only until you create the PIN file named by `ui_pin_file`
   (`mkdir -p ~/.config/gaprunner && (umask 077; echo 'pick-a-pin' > ~/.config/gaprunner/pin)`).
3. Let shadow mode watch real traffic for a while, then replay it to see what GapRunner would do:
   `gaprunner replay gaprunner.toml ~/.local/state/gaprunner/activity.jsonl`. When it looks right, set
   `mode = "live"` and restart.
4. Add jobs with `hold = true`, measure them with `gaprunner calibrate`, and start them from the page.
   [docs/RECIPES.md](docs/RECIPES.md) walks through it; [examples/gb300-workstation.toml](examples/gb300-workstation.toml)
   is a fuller config with a benchmark and a LoRA run.

To run it under systemd, start from [examples/gaprunner.service](examples/gaprunner.service) and keep
`KillMode=process`, so restarting or upgrading GapRunner leaves running jobs alone. While the gate is in your clients'
request path, also run `gaprunner watchdog` with a `bypass_cmd`
([CONFIG.md](docs/CONFIG.md#when-the-gate-stops-answering)).

## What it doesn't do

- **One GPU per instance.** It manages one GPU (by default the one with the most memory). More than one GPU is on the
  [roadmap](docs/DESIGN.md#roadmap).
- **Not a cluster scheduler.** It shares one box between a model server and background jobs; it doesn't place work
  across machines.
- **The gate is in the request path.** If GapRunner stops, requests stop unless the watchdog routes around it, and a
  bypass doesn't wake a sleeping server by itself.
- **The config is trusted.** Recipes, hooks and `notify_cmd` run as shell commands with GapRunner's permissions.
- **Jobs must be able to step aside**: resume from their own checkpoint, be freezable by cuda-checkpoint, or sleep
  like a model server. GapRunner doesn't make arbitrary code resumable, and a `share` job can slow inference.

## Docs

- [Job recipes](docs/RECIPES.md): placements, drivers, lifecycle, calibration, with examples
- [Config reference](docs/CONFIG.md): every field and its default
- [Agent guide](docs/AGENT-GUIDE.md): for AI agents on a GapRunner machine; plus the [skill](skills/gaprunner/SKILL.md) and [llms.txt](llms.txt)
- [Design](docs/DESIGN.md): the pieces, the decisions and the roadmap
- [Prior art](docs/PRIOR-ART.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [AGENTS.md](AGENTS.md) for code changes

The tests use a fake GPU and a fake vLLM server, so they run anywhere: `python3 -m unittest discover -s tests`.

## Prior art

Model-swapping brokers already exist and are good: [Berth](https://github.com/T-Crypt/berth),
[llama-swap](https://github.com/mostlygeek/llama-swap), [gpu-broker](https://github.com/emergenthq-net/gpu-broker),
[llmux](https://github.com/doublewordai/llmux) and [gpuq](https://github.com/elektricM/gpuq) take turns between
*services*. Research systems (LLMStation, SIRIUS, ConServe, DeltaServe, PipeSwitch) share a GPU between serving and
training inside one engine. GapRunner fills a model's idle gaps with arbitrary resumable jobs, and borrows openly
from these projects. Credits in [docs/PRIOR-ART.md](docs/PRIOR-ART.md).

## Security

Run GapRunner on a machine you control, on loopback or a private network. Read [SECURITY.md](SECURITY.md) before
exposing anything. In short: the config is code; the PIN guards changes only,
and anyone who can reach the page or the gate can read status and job logs; the gate passes any path through to your
model server.

## License

Apache-2.0. `cuda-checkpoint` (NVIDIA EULA) and CRIU (GPL) are called as external programs and are not included.
