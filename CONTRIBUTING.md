# Contributing

Thanks for helping. Issues and pull requests are welcome.

- **Read [AGENTS.md](AGENTS.md) first.** It has the ground rules (standard library only, no site-specific names or
  paths, services first) and the layout. It's written for AI coding agents, and it's the same for people.
- **Run the tests:** `python3 -m unittest discover -s tests`. They use a fake GPU and a fake vLLM, so no GPU is needed.
  Add a test with every change in behaviour.
- **Tested on real hardware?** Say what ran, on which GPU and with which model servers, and what you saw. It's the
  most useful part of a pull request.
- **Bigger changes:** open an issue first to talk it through. GapRunner prefers plain code over hardening for unlikely
  cases: if you're adding protection against a rare race or attack, describe the failure, how likely it is and what it
  costs to prevent.
- **Docs:** if you change a config field, update [docs/CONFIG.md](docs/CONFIG.md) and, for jobs, [docs/RECIPES.md](docs/RECIPES.md).

By contributing you agree your work is licensed under the project's Apache-2.0 license.
