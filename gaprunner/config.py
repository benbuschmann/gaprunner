"""Config: one TOML file (see examples/). Validated up front so a bad file fails at start, not at 3 a.m."""
import copy
import datetime as dt
import json
import math
import os
import tomllib

from .scheduler import Policy

TOP_DEFAULTS = {
    "mode": "shadow",            # shadow: observe, learn gaps, log what it WOULD do; live: act
    "state_dir": "~/.local/state/gaprunner",
    "tick_s": 1.0,
    "gpu_index": None,           # None: the GPU with the most memory; or an index, or a UUID "GPU-..."
    "gpu_total_gib": None,       # None: read from the driver
    "verify_free_s": 30.0,       # how long to wait for the driver to show memory free before a wake/resume
    "kill_stuck_jobs": True,     # a job that will not park within its timeout is killed so the service can wake
    "queue_file": None,          # job order + holds, edited from the web UI; default <state_dir>/queue.json
    "window_file": None,         # a window is open while this file exists (it may hold an end time); default
                                 # <state_dir>/WINDOW
    "ui_listen": None,           # web UI address, e.g. "127.0.0.1:8096"; None = no UI
    "ui_pin_file": None,         # file holding the PIN that UI changes require; None = UI is read-only
    "notify_cmd": None,          # run on health alerts with GAPRUNNER_LEVEL / _TARGET / _MSG in the environment
    "notify_file": None,         # and/or append each alert as a JSON line here (for a relay to forward)
    "windows": [],               # optional daily windows, local time, "HH:MM-HH:MM"
    "pause_file": None,          # kill switch; default <state_dir>/PAUSE
    "governor_dir": None,        # for driver = "governor" jobs: the folder apps read (client modules, reservations,
                                 # admission socket); default <state_dir>/governor
    "governor_uids": [],         # other users whose apps may ask (Linux); GapRunner's own user always may
}
SERVICE_REQUIRED = ("name", "driver", "upstream", "listen")
ENGINE_REQUIRED = ("name", "driver", "upstream")
SERVICE_ONLY = ("name", "listen", "idle_park_s", "passive_paths", "bypass_cmd", "restore_cmd", "auto_restore",
                "schedule", "default_engine", "engines", "request_timeout_s")
JOB_REQUIRED = ("name", "driver")
TIERS = ("hbm_gib", "cpu_gib", "ssd_gib")       # fast GPU memory, CPU memory, disk
COEXIST = ("no", "share", "yield")


#: The page's job colours (light and dark variants are checked for colour-blind contrast); a recipe may pick one.
JOB_COLORS = ("blue", "orange", "teal", "purple", "olive", "pink", "indigo", "amber")


class ConfigError(ValueError):
    pass


def expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def load(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return parse(raw)


def parse(raw):
    # work on a copy: parsing fills in defaults, and a caller may parse the same dict twice
    # (keep live objects such as a test's fake GPU by reference)
    raw = copy.copy(raw)
    raw["service"] = [dict(s) for s in raw.get("service", [])]
    raw["job"] = [dict(j) for j in raw.get("job", [])]
    top = {**TOP_DEFAULTS, **raw.get("gaprunner", {})}
    if top["mode"] not in ("shadow", "live"):
        raise ConfigError("gaprunner.mode must be shadow or live")
    top["state_dir"] = expand(top["state_dir"])
    top["pause_file"] = expand(top["pause_file"]) or os.path.join(top["state_dir"], "PAUSE")
    top["queue_file"] = expand(top["queue_file"]) or os.path.join(top["state_dir"], "queue.json")
    top["window_file"] = expand(top["window_file"]) or os.path.join(top["state_dir"], "WINDOW")
    top["ui_pin_file"] = expand(top["ui_pin_file"])
    top["notify_file"] = expand(top["notify_file"])
    top["governor_dir"] = expand(top["governor_dir"]) or os.path.join(top["state_dir"], "governor")
    if not isinstance(top["governor_uids"], list) or not all(isinstance(u, int) for u in top["governor_uids"]):
        raise ConfigError("gaprunner.governor_uids must be a list of numeric user ids")
    for w in top["windows"]:
        _parse_window(w)
    pol = raw.get("policy", {})
    unknown = set(pol) - set(Policy.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown policy keys: {', '.join(sorted(unknown))}")
    policy = Policy(**pol)
    if not isinstance(policy.max_active_jobs, int) or policy.max_active_jobs < 0:
        raise ConfigError("policy.max_active_jobs must be a whole number, 0 for no limit")
    services, jobs, names = raw.get("service", []), raw.get("job", []), set()
    claims = raw.get("claim", [])
    for c in claims:
        _need(c, ("name", "action"), "claim")
        if c["action"] not in ("hold", "track"):
            raise ConfigError(f"claim {c['name']}: action must be hold or track")
        if not (c.get("pgrep") or c.get("pids_cmd")):
            raise ConfigError(f"claim {c['name']}: give pgrep or pids_cmd to find its processes")
        c.setdefault("linger_s", 15.0)
    if not services:
        raise ConfigError("at least one [[service]] is required")
    for s in services:
        _need(s, ("name", "listen"), "service")
        s.setdefault("idle_park_s", 30.0)
        if s.get("engines"):
            # one service (one gate, one alias) with switchable engines; each engine is a recipe of its own
            base = {k: v for k, v in s.items() if k not in SERVICE_ONLY}
            engines = []
            for e in s["engines"]:
                _need(e, ENGINE_REQUIRED, f"service {s['name']} engine")
                engines.append(_engine({**base, **e}, s, e["name"]))
            engine_names = [e["engine_name"] for e in engines]
            if len(set(engine_names)) != len(engine_names):
                raise ConfigError(f"service {s['name']}: engine names must be unique")
            for e in engines:
                if not (e.get("start_cmd") and e.get("stop_cmd")):
                    raise ConfigError(f"service {s['name']} engine {e['engine_name']}: a switchable engine needs "
                                      "start_cmd and stop_cmd (a switched-out engine is stopped)")
                e.setdefault("switch_out", "stop")
                if e["switch_out"] not in ("stop", "sleep"):
                    raise ConfigError(f"service {s['name']} engine {e['engine_name']}: switch_out is stop or sleep")
            s["engines"] = engines
            s.setdefault("default_engine", engine_names[0])
            if s["default_engine"] not in engine_names:
                raise ConfigError(f"service {s['name']}: default_engine {s['default_engine']!r} is not one of its engines")
        else:
            _need(s, SERVICE_REQUIRED, "service")
            s["engines"] = [_engine(dict(s), s, "default")]
            s["default_engine"] = "default"
        for slot in s.get("schedule", []):
            if slot.get("engine") not in [e["engine_name"] for e in s["engines"]]:
                raise ConfigError(f"service {s['name']}: schedule engine {slot.get('engine')!r} is not one of its engines")
            try:
                dt.time.fromisoformat(slot["at"])
            except (KeyError, ValueError):
                raise ConfigError(f"service {s['name']}: schedule entries need at = \"HH:MM\"") from None
    for i, j in enumerate(jobs):
        job_defaults(j, i)
    owners = [j["owner"] for j in jobs if j["driver"] == "governor"]
    if len(set(owners)) != len(owners):
        raise ConfigError("two governor jobs have the same owner")
    for x in services + jobs:
        if x["name"] in names:
            raise ConfigError(f"duplicate name {x['name']}")
        names.add(x["name"])
    svc_engines = {s["name"]: [e["engine_name"] for e in s["engines"]] for s in services}
    for j in jobs:
        if j.get("target_service") and j["target_service"] not in svc_engines:
            raise ConfigError(f"job {j['name']}: target_service {j['target_service']!r} is not a service")
        if j.get("requires_engine"):
            svc = j.get("target_service") or (services[0]["name"] if len(services) == 1 else None)
            if not svc or j["requires_engine"] not in svc_engines[svc]:
                raise ConfigError(f"job {j['name']}: requires_engine {j['requires_engine']!r} is not an engine of its target_service")
            j["target_service"] = svc
        j["after"] = [j["after"]] if isinstance(j.get("after"), str) else list(j.get("after", []))
        for dep in j["after"]:
            if dep not in {x["name"] for x in jobs}:
                raise ConfigError(f"job {j['name']}: after = {dep!r} is not a job in this config")
    from . import jobsapi
    templates = jobsapi.parse_templates([dict(t) for t in raw.get("template", [])], ConfigError)
    clients = jobsapi.parse_clients(raw.get("client", []), templates, ConfigError)
    return {"gaprunner": top, "policy": policy, "services": services, "jobs": jobs, "claims": claims,
            "templates": templates, "clients": clients}


def _engine(e, service, name):
    """The full spec of one engine of a service (service-wide fields plus the engine's own)."""
    e = dict(e)
    e["engine_name"] = name
    e["name"] = service["name"]
    e["listen"] = service["listen"]
    _footprint(e, f"service {service['name']} engine {name}")
    if bool(e.get("stop_cmd")) != bool(e.get("start_cmd")):
        raise ConfigError(f"service {service['name']} engine {name}: stop_cmd and start_cmd go together")
    e.setdefault("wake_s", 1.0)
    e.setdefault("park_s", 1.0)
    e.setdefault("url", e["upstream"])
    e.setdefault("ready_timeout_s", 1800)
    e.setdefault("restart_on_failed_wake", True)
    return e


def job_defaults(j, i):
    """Check one [[job]] entry and fill in its defaults (also used for jobs submitted through the Jobs API)."""
    _need(j, JOB_REQUIRED, "job")
    _footprint(j, "job")
    j.setdefault("coexist", "no")
    if j["coexist"] not in COEXIST:
        raise ConfigError(f"job {j['name']}: coexist must be one of {', '.join(COEXIST)}")
    j.setdefault("yield_resume_s", 5.0)
    if j.get("fallback") not in (None, "restart"):
        raise ConfigError(f"job {j['name']}: fallback can only be \"restart\"")
    if j.get("fallback") and not j.get("start_cmd"):
        raise ConfigError(f"job {j['name']}: fallback = \"restart\" needs a start_cmd to run it again")
    if j["driver"] == "governor":
        _need(j, ("owner",), "job")
        if j.get("start_cmd"):
            raise ConfigError(f"job {j['name']}: a governor job is started by its app, not by a start_cmd")
        j.setdefault("admit_wait_s", 300.0)   # how long a request may wait in the queue before it is refused
        j.setdefault("grace_s", 30.0)         # after a revoke: SIGTERM, then SIGKILL after this long
        # a revoke stops the app's work in progress (a render is lost), so it counts as losing work
        j.setdefault("loses_work", True)
    j.setdefault("park_s", 5.0)
    j.setdefault("resume_s", 5.0)
    # a job that loses its unsaved work when it steps aside (stopped and started again) needs a quiet spell long
    # enough to reach its next saved point; restart jobs do by default
    j.setdefault("loses_work", j["driver"] == "restart")
    if not isinstance(j["loses_work"], bool):
        raise ConfigError(f"job {j['name']}: loses_work must be true or false")
    if j.get("save_every_s") is not None and float(j["save_every_s"]) <= 0:
        raise ConfigError(f"job {j['name']}: save_every_s must be a positive number of seconds")
    j.setdefault("order", i)
    j.setdefault("hold", False)
    j.setdefault("window_only", False)
    j.setdefault("enabled", True)
    if j.get("color") is not None and j["color"] not in JOB_COLORS:
        raise ConfigError(f"job {j['name']}: color must be one of {', '.join(JOB_COLORS)}")
    return j


def _footprint(e, kind):
    """Memory per tier while running and while parked. `gpu_gib` is shorthand for running.hbm_gib.
    Unknown values stay None (shown as "not declared"); parked HBM defaults to 0."""
    run, park = dict(e.get("running", {})), dict(e.get("parked", {}))
    for t in list(run) + list(park):
        if t not in TIERS:
            raise ConfigError(f"{kind} {e.get('name', '?')}: unknown tier {t} (use {', '.join(TIERS)})")
    run.setdefault("hbm_gib", e.get("gpu_gib"))
    if run["hbm_gib"] is None:
        raise ConfigError(f"{kind} {e.get('name', '?')}: running.hbm_gib (or gpu_gib) is required")
    park.setdefault("hbm_gib", 0.0)
    e["running"] = {t: run.get(t) for t in TIERS}
    e["parked"] = {t: park.get(t) for t in TIERS}
    e["gpu_gib"] = float(run["hbm_gib"])


def _need(entry, keys, kind):
    missing = [k for k in keys if k not in entry]
    if missing:
        raise ConfigError(f"{kind} {entry.get('name', '?')}: missing {', '.join(missing)}")


def _parse_window(w):
    try:
        a, b = w.split("-")
        return dt.time.fromisoformat(a.strip()), dt.time.fromisoformat(b.strip())
    except ValueError:
        raise ConfigError(f"bad window {w!r}; use HH:MM-HH:MM") from None


def window_file_info(top):
    """The UI-opened window: {"until": epoch or 0 (open-ended), "exclusive": bool}, or None when there is none.
    Exclusive windows stop the services that have a stop_cmd, so jobs can have the whole GPU."""
    try:
        with open(top["window_file"]) as f:
            txt = f.read().strip()
    except (OSError, KeyError, TypeError):
        return None
    try:
        v = json.loads(txt) if txt else {}
    except ValueError:
        v = {}
    if type(v) in (int, float):
        v = {"until": v}
    try:
        until, exclusive = v.get("until", 0), v.get("exclusive", False)
        if isinstance(until, bool) or not isinstance(exclusive, bool):
            return None
        until = float(until)
        if not math.isfinite(until):
            return None
        return {"until": until, "exclusive": exclusive}
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None          # not a window (a list, "until": "06:00"): no window rather than an open-ended one


def window_until(top):
    w = window_file_info(top)
    return None if w is None else w["until"]


def exclusive_window(top, now=None):
    """True while a UI-opened exclusive window is open."""
    w = window_file_info(top)
    t = (now or dt.datetime.now()).timestamp()
    return bool(w and w["exclusive"] and (w["until"] == 0 or w["until"] > t))


def window_open(top, now=None):
    until = window_until(top)
    if until is not None and (until == 0 or until > (now or dt.datetime.now()).timestamp()):
        return True
    t = (now or dt.datetime.now()).time()
    for w in top.get("windows", []):
        a, b = _parse_window(w)
        if (a <= t < b) if a <= b else (t >= a or t < b):
            return True
    return False


def read_queue(top):
    try:
        with open(top["queue_file"]) as f:
            q = json.load(f)
    except (OSError, ValueError, KeyError, TypeError):
        return {"jobs": []}
    if not isinstance(q, dict) or not isinstance(q.get("jobs"), list):
        return {"jobs": []}                       # not a queue GapRunner wrote: no overrides
    q["jobs"] = [x for x in q["jobs"] if isinstance(x, dict) and isinstance(x.get("name"), str)]
    return q


def write_queue(top, q):
    tmp = top["queue_file"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(q, f, indent=1)
    os.replace(tmp, top["queue_file"])


def queue_overrides(top):
    """{job name: {"order", "hold", "window_only", "coexist", "after"}} from the queue file; {} if none or unreadable.
    coexist and after are None when the queue file doesn't set them (the recipe's value applies)."""
    out = {}
    for i, j in enumerate(read_queue(top).get("jobs", [])):
        if j.get("name"):
            out[j["name"]] = {"order": i, "hold": bool(j.get("hold")), "window_only": bool(j.get("window_only")),
                              "coexist": j.get("coexist") if j.get("coexist") in ("no", "share", "yield") else None,
                              "after": [str(x) for x in j["after"]] if isinstance(j.get("after"), list) else None,
                              "tags": [str(x) for x in j["tags"]] if isinstance(j.get("tags"), list) else None}
    return out
