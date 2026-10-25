# AGENTS.md — working on the GapRunner code

If you are an agent *using* GapRunner on a machine, read `docs/AGENT-GUIDE.md` instead. This file is for changing the code.

## Ground rules
- **Open-source first.** No site-specific hosts, paths, names or credentials in code or docs; they belong in a config under `examples/` only as clearly labelled examples. Defaults must be sensible on a stranger's machine.
- **Standard library only** (Python 3.11+). Any new dependency needs a strong reason and a permissive licence.
- **Keep it plain.** Don't add hardening for unlikely races or edge cases without first writing down the failure, its likelihood and its cost, and getting the maintainer's agreement.
- **Services first** is the invariant every change must keep: a service request is never answered by the wrong engine, outside slow-wake periods only jobs that step aside within `max_added_wait_s` run while a service sleeps, and a job stuck stepping aside is stopped hard at `wake_deadline_s`.

## Layout
- `scheduler.py` is pure (no I/O, no clocks). The daemon and `replay` call the same functions, so put decision logic here and test it with plain snapshots.
- `runner.py` owns every transition under one lock. Drivers do I/O; they never decide.
- `ui.html` is one self-contained file (no CDN, no build step); charts are inline SVG.

## Tests
```bash
python3 -m unittest discover -s tests
```
`tests/fakes.py` provides a fake vLLM server and a fake GPU, so the suite runs on any laptop with no GPU. Add a test with every behaviour change. Live tests on real hardware go in the PR description (what ran, which GPU, the numbers).

## Releases
Bump `__version__` in `gaprunner/__init__.py` and `version` in `pyproject.toml` with every release tag (the web page shows it), and add the release to `CHANGELOG.md`.

## Commits
One change per commit. Say in the body what changed in behaviour and how it was verified.
