"""gaprunner run | status | check | example | switch | calibrate | replay | adapter | watchdog | pause | unpause"""
import argparse
import dataclasses
import json
import os
import sys
import urllib.error
import threading

from . import __version__
from . import config as C


def cmd_run(a):
    from . import gate
    from .runner import Runner
    r = Runner(C.load(a.config))
    for name in r.svc:
        srv = gate.serve(r, name)
        threading.Thread(target=srv.serve_forever, daemon=True, name=f"gate-{name}").start()
        print(f"gate {name}: {r.svc[name]['spec']['listen']} -> {r.svc[name]['spec']['upstream']}", flush=True)
    if r.top.get("ui_listen"):
        from . import ui
        srv = ui.serve(r)
        threading.Thread(target=srv.serve_forever, daemon=True, name="ui").start()
        print(f"ui: http://{r.top['ui_listen']}/", flush=True)
    print(f"gaprunner {__version__} mode={r.top['mode']} state={r.top['state_dir']}", flush=True)
    r.run_forever()


def cmd_status(a):
    top = C.load(a.config)["gaprunner"]
    with open(os.path.join(top["state_dir"], "status.json")) as f:
        print(json.dumps(json.load(f), indent=1))


def cmd_example(a):
    """Print a minimal config to start from: gaprunner example > gaprunner.toml"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "example.toml")) as f:
        sys.stdout.write(f.read())


def cmd_check(a):
    """Validate the config, then read the GPU and every service and job once. Writes nothing; it does run the
    recipes' inspection commands (pids_cmd, pgrep, state_cmd), which are trusted code like the rest of the config.
    Exit 0 when the config is valid; with --strict, also exit 1 unless the GPU is readable and every service answers."""
    from .runner import Runner
    from .scheduler import UNKNOWN
    r = Runner(C.load(a.config), readonly=True)
    r.inspect()
    st = r.status()
    print(json.dumps(st, indent=1, default=str))
    problems = []
    if not st.get("gpu"):
        which = "the default GPU" if r.top["gpu_index"] is None else f"GPU {r.top['gpu_index']}"
        problems.append(f"can't read {which} (is nvidia-smi installed and working, and is gpu_index right?)")
    for n, v in st["services"].items():
        if v["state"] == UNKNOWN:
            up = next((x.get("upstream") for x in (v.get("engines") or {}).values() if x.get("current")), None)
            problems.append(f"service {n} doesn't answer at {up or 'its upstream'}; start it first")
    for p in problems:
        print(f"not ready: {p}", file=sys.stderr)
    no_goal = [n for n, j in r.job.items() if not j["spec"].get("goal")]
    if no_goal:
        print(f"note: no goal written for: {', '.join(no_goal)} (say what each job is for and what done means)",
              file=sys.stderr)
    if not problems:
        print("ready: the GPU is readable and every service answers", file=sys.stderr)
    if problems and a.strict:
        sys.exit(1)


def cmd_replay(a):
    from . import replay
    cfg = C.load(a.config)
    ev = replay.load_yield(a.trace) if a.format == "yield" else replay.load_requests(a.trace)
    if not ev:
        sys.exit("no trace segment has 2 or more events")
    pol = cfg["policy"]
    over = {k: v for k, v in (("p_min", a.p_min), ("worth_factor", a.worth)) if v is not None}
    pol = dataclasses.replace(pol, **over)
    if a.idle_park is not None:
        cfg["services"][0]["idle_park_s"] = a.idle_park
    print(json.dumps(replay.simulate(cfg, ev, policy=pol), indent=1))


def cmd_adapter(a):
    from . import adapter, drivers
    spec = {"name": a.name, "driver": a.driver, "url": a.url, "pgrep": a.pgrep, "pid_file": a.pid_file,
            "sudo": a.sudo, "park_cmd": a.park_cmd, "resume_cmd": a.resume_cmd, "state_cmd": a.state_cmd}
    srv = adapter.serve(drivers.make({k: v for k, v in spec.items() if v is not None}), a.listen)
    print(f"park protocol for {a.name} ({a.driver}) on {a.listen}", flush=True)
    srv.serve_forever()


def cmd_calibrate(a):
    """Ask the running daemon to calibrate a job (needs live mode and the UI PIN file)."""
    import urllib.request
    top = C.load(a.config)["gaprunner"]
    if not top.get("ui_listen") or not top.get("ui_pin_file"):
        sys.exit("calibrate goes through the daemon's UI API: set ui_listen and ui_pin_file")
    body = {"action": "calibrate", "name": a.job, "warm_s": a.warm, "cycles": a.cycles,
            "pin": open(top["ui_pin_file"]).read().strip()}
    req = urllib.request.Request(f"http://{top['ui_listen']}/api/action", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        print(json.load(urllib.request.urlopen(req, timeout=10)).get("did"))
    except urllib.error.HTTPError as e:
        sys.exit(json.loads(e.read() or b"{}").get("error", str(e)))
    print("progress and results: the UI (Jobs, click the job) or <state_dir>/calibration/")


def cmd_switch(a):
    """Ask the running daemon to switch a service to another engine (needs ui_listen and ui_pin_file)."""
    import urllib.request
    top = C.load(a.config)["gaprunner"]
    body = {"action": "switch", "service": a.service, "engine": a.engine, "pin": open(top["ui_pin_file"]).read().strip()}
    req = urllib.request.Request(f"http://{top['ui_listen']}/api/action", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        print(json.load(urllib.request.urlopen(req, timeout=10)).get("did"))
    except urllib.error.HTTPError as e:
        sys.exit(json.loads(e.read() or b"{}").get("error", str(e)))


def cmd_watchdog(a):
    from . import watchdog
    watchdog.run(C.load(a.config))


def cmd_pause(a, on=True):
    p = C.load(a.config)["gaprunner"]["pause_file"]
    if on:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        print(f"paused ({p}): services wake, jobs park, nothing is scheduled")
    elif os.path.exists(p):
        os.remove(p)
        print("unpaused")


def cmd_token(a):
    """A new Jobs API token and the line for its [[client]] entry. The token is shown once; the config keeps only
    its SHA-256."""
    from .jobsapi import token_pair
    token, digest = token_pair()
    print(f"token (give this to the client, once):\n  {token}\n\nfor its [[client]] entry:\n  token_sha256 = \"{digest}\"")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gaprunner", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n, f in (("run", cmd_run), ("status", cmd_status), ("check", cmd_check), ("watchdog", cmd_watchdog), ("pause", cmd_pause),
                 ("unpause", lambda a: cmd_pause(a, False))):
        p = sub.add_parser(n)
        p.add_argument("config")
        if n == "check":
            p.add_argument("--strict", action="store_true", help="also fail unless the GPU is readable and every service answers")
        p.set_defaults(fn=f)
    sub.add_parser("example", help="print a minimal config to start from").set_defaults(fn=cmd_example)
    sub.add_parser("token", help="make a Jobs API token and the hash for its [[client]] entry").set_defaults(fn=cmd_token)
    p = sub.add_parser("switch")
    p.add_argument("config")
    p.add_argument("service")
    p.add_argument("engine")
    p.set_defaults(fn=cmd_switch)
    p = sub.add_parser("calibrate")
    p.add_argument("config")
    p.add_argument("job")
    p.add_argument("--warm", type=float, default=60.0, help="seconds to run before each park")
    p.add_argument("--cycles", type=int, default=2)
    p.set_defaults(fn=cmd_calibrate)
    p = sub.add_parser("replay")
    p.add_argument("config")
    p.add_argument("trace", nargs="+")
    p.add_argument("--format", choices=("yield", "requests"), default="requests")
    p.add_argument("--p-min", type=float)
    p.add_argument("--worth", type=float)
    p.add_argument("--idle-park", type=float)
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("adapter")
    p.add_argument("--name", default="job")
    p.add_argument("--driver", required=True)
    p.add_argument("--listen", required=True)
    for k in ("url", "pgrep", "pid-file", "park-cmd", "resume-cmd", "state-cmd"):
        p.add_argument(f"--{k}")
    p.add_argument("--sudo", action="store_true")
    p.set_defaults(fn=cmd_adapter)
    a = ap.parse_args(argv)
    a.fn(a)
