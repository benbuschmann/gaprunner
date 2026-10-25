---
name: gaprunner
description: Use on any machine where GapRunner manages the GPU — before starting, stopping or killing GPU work, when GPU memory looks taken, when asked why a job isn't running, or when adding a training/benchmark/eval job. Teaches the rules (never kill GPU processes you didn't start), how to read GapRunner's state, and how to queue work as a recipe.
---

# GapRunner

GapRunner shares one GPU between always-on AI services (which come first) and background jobs that run in the gaps. On a machine it manages:

1. **Don't kill, stop or restart GPU processes you didn't start**, and don't reset the GPU. Look up the holder first: `curl -s <ui>/api/status`, where `<ui>` is `ui_listen` from the GapRunner config.
2. **Don't start GPU work directly.** Write a recipe (`[[job]]` in the config) with `hold = true`, run `gaprunner check <config>`, and ask the operator to release it.
3. **Why isn't a job running?** Read `jobs[<name>].why` in `/api/status`. It names the blocker in plain words.
4. **Emergency only:** `gaprunner pause <config>` wakes the services and parks every job. Tell the operator immediately.

Reference (in the GapRunner repo): `docs/AGENT-GUIDE.md` (rules, API, workflow), `docs/RECIPES.md` (recipe fields, drivers, lifecycle), `README.md`.
