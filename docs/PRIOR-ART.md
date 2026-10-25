# Prior art and credits

GapRunner borrows ideas openly. Thank you to these projects.

| Project | What we took |
|---|---|
| [Berth](https://github.com/T-Crypt/berth) (Apache-2.0) | The park protocol (`/berth/state`, `/berth/park`, `/berth/resume`), which we implement as a client and serve with `gaprunner adapter`. Trusting the driver's memory reading over the engine's word. Stowaway reporting. The anti-thrash ("juggle") window. Per-tier measured switch costs. |
| [gpu-broker](https://github.com/emergenthq-net/gpu-broker) (Apache-2.0) | Replaying a real log through the same scheduler rules in simulated time. The starvation guard. |
| [llmux](https://github.com/doublewordai/llmux) (MIT) | Shell hooks (wake / sleep / alive) as the universal driver: our `command` driver. Draining before a switch. |
| [llama-swap](https://github.com/mostlygeek/llama-swap) (MIT) | The vLLM sleep/wake wrapper pattern. GapRunner sits beside llama-swap; point a llama-swap alias at a GapRunner gate. |
| [vllm-sleeper-proxy](https://github.com/SailorJoe6/vllm-sleeper-proxy) | Failing closed: reconcile real engine state at startup. |
| [model-swap-proxy](https://github.com/Suppressor72/model-swap-proxy) (MIT) | Concurrent requests share one switch. |
| [gpuq](https://github.com/elektricM/gpuq) (GPL-3.0, ideas only) | An HTTP gate that holds requests while the GPU is busy. |
| [qui](https://github.com/autobrr/qui) (GPL-2.0, ideas only) and qBittorrent's web UI | Managing a long queue: status filters, select many and act on them, drag to reorder, clear finished, a details pane under the list, live updates without page reloads. |
| [LIMCARS](https://github.com/AgentSparko/LIMCARS) (ideas only) | Engine stats read from Prometheus metrics (time to first token, KV use, prefix-cache hits), showing the last real speed when idle, and naming model servers GapRunner doesn't manage. |
| vLLM sleep mode, SGLang memory_occupation, NVIDIA cuda-checkpoint, CRIU | The mechanisms the drivers call. |
| LLMStation, SIRIUS, ConServe, DeltaServe, PipeSwitch, Salus (papers) | Evidence that serving/training time-sharing works. Layer-boundary yielding and incremental checkpoints are on our roadmap for shorter reclaim times. |

No code was copied from these projects; where we borrowed, it was the idea. If you know of a project that does what GapRunner does, please open an issue so we can credit it and learn from it.
