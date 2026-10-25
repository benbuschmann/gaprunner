"""Watchdog: keeps a service reachable if GapRunner itself stops answering.

    gaprunner watchdog <config>          (run it as its own service, next to `gaprunner run`)

Every `watchdog_every_s` (5) it asks each gate for /gaprunner/state. After `watchdog_failures` (3) misses in a row
it runs that service's `bypass_cmd` once (e.g. point the router straight at the engine and reload it), logs and
notifies. When the gate answers again it says so and, if the service has `restore_cmd` and `auto_restore = true`,
runs it after the gate has been healthy for 60 s; otherwise a person restores the route.
"""
import json
import os
import subprocess
import time
import urllib.request


def _ok(listen):
    try:
        with urllib.request.urlopen(f"http://{listen}/gaprunner/state", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def _log(top, msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(top["state_dir"], "watchdog.log"), "a") as f:
        f.write(line + "\n")
    from .health import write_notify_file
    write_notify_file(top, "warn", "watchdog", msg)
    if top.get("notify_cmd"):
        subprocess.Popen(["sh", "-c", top["notify_cmd"]], env={**os.environ, "GAPRUNNER_LEVEL": "warn",
                         "GAPRUNNER_TARGET": "watchdog", "GAPRUNNER_MSG": msg}, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(cfg, rounds=None):
    top = cfg["gaprunner"]
    every, limit = float(top.get("watchdog_every_s", 5)), int(top.get("watchdog_failures", 3))
    watched = [s for s in cfg["services"] if s.get("bypass_cmd")]
    if not watched:
        raise SystemExit("no service has a bypass_cmd; nothing to watch")
    state = {s["name"]: {"miss": 0, "bypassed": False, "healthy_since": None} for s in watched}
    n = 0
    while rounds is None or n < rounds:
        n += 1
        for s in watched:
            st = state[s["name"]]
            if _ok(s["listen"]):
                st["miss"] = 0
                if st["bypassed"]:
                    st["healthy_since"] = st["healthy_since"] or time.time()
                    if s.get("restore_cmd") and s.get("auto_restore") and time.time() - st["healthy_since"] >= 60:
                        r = subprocess.run(["sh", "-c", s["restore_cmd"]], capture_output=True, text=True, timeout=300)
                        _log(top, f"{s['name']}: gate healthy 60 s; restore_cmd exit {r.returncode}")
                        st["bypassed"], st["healthy_since"] = False, None
                    elif not st.get("told"):
                        _log(top, f"{s['name']}: gate answers again; requests still bypass it until restored")
                        st["told"] = True
                continue
            st["miss"] += 1
            st["healthy_since"] = None
            if st["miss"] >= limit and not st["bypassed"]:
                r = subprocess.run(["sh", "-c", s["bypass_cmd"]], capture_output=True, text=True, timeout=300)
                st["bypassed"], st["told"] = r.returncode == 0, False
                _log(top, f"{s['name']}: gate did not answer {st['miss']} times; bypass_cmd exit {r.returncode}"
                          + ("" if r.returncode == 0 else f": {(r.stderr or r.stdout).strip()[:200]}"))
        time.sleep(every)
    return state
