# Design

## Pieces

```
clients ──> gate (one per service) ──> engine (vLLM, SGLang, ...)
              │ request_start / ensure_hot / request_end
              v
            Runner ── tick every 1 s ──> scheduler.decide(snapshot)   (pure, also used by replay)
              │                           -> park_service / resume_job / park_job / wake_service
              ├── drivers (vllm, sglang, park-protocol, cuda-checkpoint, command)
              ├── gpu (nvidia-smi: total, used, per-process) -> wait_free, stowaways
              ├── GapModel per service (conditional idle survival, learned live, persisted)
              ├── CostBook (last 20 measured park/resume times per target, seeded from config)
              └── audit.jsonl, status.json, costs.json, gaps-<service>.json in state_dir
```

## Decisions

- **The scheduler is a pure function** of a snapshot. The daemon and `replay` run identical rules, so a replay is a faithful preview of a policy change.
- **Services first, without exceptions.** A service wake parks every job that doesn't fit beside the service (jobs that fit keep running, pausing their compute while it answers if they're set to give way). A job that won't step aside by the wake deadline is stopped hard when allowed, and the wake proceeds even if memory looks overlapped (logged as `overlap_risk`).
- **Park a service only when it pays.** An idle service is parked only if some waiting job would be admitted into the current gap. Otherwise it stays hot and its next request costs nothing.
- **Gap test.** For each job: P(idle ≥ worth_factor × (park + resume) more | idle so far) ≥ p_min, using the minimum across services. With fewer than 20 observed gaps, it falls back to a fixed rule (idle ≥ cold_start_after_s).
- **Reclaim limit.** A job whose park time plus the slowest service wake exceeds `max_added_wait_s` is windows-only, because a request would wait that long.
- **Shadow mode first.** Requests pass through, gaps are learned and intended actions are logged. Going live is a config change.

## Roadmap

Done: shadow mode and replay; jobs beside a service (0.3); the web page (queue, history, details, keyboard); engine
switching; health and alerts; per-job memory and timing measurements; a deadline on request wakes (0.16).

Next:
1. Reclaim latency: drivers for training loops that yield at a layer or step boundary.
2. More than one GPU, and services that are used rarely with their own gates.
3. Push updates to the page instead of polling.
4. Upstream conversation with Berth: our adapters already speak its protocol.
