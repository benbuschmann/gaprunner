# Security

## Reporting a problem

Please report security problems privately, not in a public issue: use GitHub's "Report a vulnerability" button on
this repository (Security tab). Expect a first answer within a week.

## The trust model

GapRunner runs on a machine you control and is meant to be reached only from that machine or a private network you
trust. It is not built to face the internet.

- **The config is code.** Job recipes (`start_cmd`, `park_cmd`, `pids_cmd`, `progress_cmd`, `health_cmd`, ...),
  service hooks (`start_cmd`, `stop_cmd`, `release_cmd`, `bypass_cmd`) and `notify_cmd` run as shell commands with
  GapRunner's user and permissions. Only people you'd give a shell to should be able to edit the config or the files
  it points to. `gaprunner check` runs the inspection commands too. Shadow mode doesn't sandbox anything.
- **Privileges.** Give GapRunner's account only what its recipes need. If a recipe uses `sudo` (for example
  `cuda-checkpoint`, or `docker` for a containerised server), allow exactly those commands in sudoers rather than
  passwordless sudo for everything.
- **The web page.** `ui_listen` is off by default; the example binds it to `127.0.0.1`. Changes need the PIN in
  `ui_pin_file` (a missing or empty file makes the page read-only). The PIN travels over plain HTTP and the browser
  keeps it in local storage, and there is no limit on guesses. Everything you can read on the page (status,
  commands, paths, job logs, model servers found on the machine) needs no PIN. Put it behind an authenticated proxy
  or a private network (for example Tailscale) if anyone else can reach the address.
- **The gate.** The gate at each service's `listen` passes every path through to the model server, including
  administrative ones, and adds no authentication of its own. `/gaprunner/state` on the gate needs no credentials.
  Keep `listen` on loopback or a trusted network, as you would the model server itself.
- **The Berth adapter** (`gaprunner adapter`) has no authentication; bind it to loopback.
- **Local discovery.** GapRunner lists listening ports on the machine and asks unknown ones for `/v1/models` (a
  read-only GET) to name model servers it doesn't manage. Nothing is sent off the machine.
