"""Calibration: run a job for real and measure what its recipe needs.

    gaprunner calibrate <config> <job>        (or Measure in a job's details in the UI; both go through the daemon)

What it does, owned by the daemon so services still come first:
  1. start (or resume) the job, even if it is held, when it fits on the GPU now;
  2. warm: sample GPU memory (per process, from the driver) and CPU memory (resident size) every second for warm_s;
  3. park it with its driver, timing the park, then sample what it still holds while parked;
  4. resume it, timing until its GPU memory is back to 90% of the steady level;
  5. repeat for `cycles`, then leave it parked, measure disk use of the recipe's ssd_paths, and write
     <state_dir>/calibration/<job>.json with the measurements and a ready-to-paste TOML block.
A service request during calibration parks the job as usual; calibration then stops and keeps what it measured.
Swap times also go into the cost book, so the scheduler uses them right away.
"""
import json
import os
import statistics
import subprocess
import time

from .scheduler import HOT, PARKED

RESULT_DIR = "calibration"


def path(runner, name):
    from .runner import _slug
    return os.path.join(runner.top["state_dir"], RESULT_DIR, f"{_slug(name)}.json")


def load(runner, name):
    try:
        with open(path(runner, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sample(runner, name):
    from .runner import _rss_gib
    j = runner.job[name]
    pids = set(j["driver"].pids())
    r = runner.gpu.read(fresh=True) or {}
    hbm = sum(p["gib"] for p in r.get("procs", []) if p["pid"] in pids)
    return round(hbm, 2), _rss_gib(list(pids)) or 0.0


def _du_gib(paths):
    total = 0
    for p in paths:
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            continue
        r = subprocess.run(["du", "-sk", p], capture_output=True, text=True, timeout=600)
        try:
            total += int(r.stdout.split()[0])
        except (ValueError, IndexError):
            pass
    return round(total / 1024 / 1024, 1)


def snippet(name, m):
    """TOML to paste into the job's [[job]] entry (unmeasured values are left as comments)."""
    run, park = m["running"], m["parked"]
    val = lambda k, v: f"{k} = {v}" if v is not None else f"# {k} = ?   (not measured)"
    ssd = (lambda v: val("ssd_gib", v)) if m.get("ssd_gib") is not None else (lambda v: "# ssd_gib = ?   (list ssd_paths in the recipe to measure)")
    est = (f'# progress: about {m["gpu_hours_total"]} GPU-hours for the whole job, {m["gpu_hours_left"]} left'
           if m.get("gpu_hours_total") else "# progress: add a progress_cmd to estimate GPU-hours")
    return "\n".join([
        f'# measured by gaprunner calibrate, {time.strftime("%Y-%m-%d %H:%M", time.localtime(m["t"]))}', est,
        val("park_s", m["park_s"]), val("resume_s", m["resume_s"]), "",
        "[job.running]", f'{val("hbm_gib", run["hbm_gib"])}        # peak {m["running_peak"]["hbm_gib"]}',
        f'{val("cpu_gib", run["cpu_gib"])}        # peak {m["running_peak"]["cpu_gib"]}', ssd(run["ssd_gib"]), "",
        "[job.parked]", val("hbm_gib", park["hbm_gib"]), val("cpu_gib", park["cpu_gib"]), ssd(park["ssd_gib"]), ""])


def run(runner, name, warm_s=60.0, cycles=2, settle_s=10.0, poll_s=1.0):
    j = runner.job[name]
    sp = j["spec"]
    m = {"job": name, "t": time.time(), "status": "running", "phase": "starting", "cycles": [], "notes": [],
         "goal": sp.get("goal"), "progress": []}
    runner.calibrating[name] = m
    runner.audit("calibrate_start", target=name, warm_s=warm_s, cycles=cycles)

    def done(status, note=None):
        m["status"], m["phase"] = status, "finished"
        if note:
            m["notes"].append(note)
        _finish(runner, name, m)
        runner.calibrating.pop(name, None)
        runner.audit("calibrate_end", target=name, status=status, note=note)
        return m

    def interrupted():
        return j["state"] != HOT or runner.paused()

    # 1. start, only if it fits now (services first: calibration never parks a service)
    need = float(sp["running"]["hbm_gib"] or 0)
    reading = runner.gpu.read(fresh=True) or {}
    free = reading.get("total_gib", 0) - reading.get("used_gib", 0) - runner.policy.reserve_gib
    with runner.lock:
        if runner.paused():
            return done("refused", "GapRunner is paused")
        if j["state"] != HOT:
            if need > free:
                sv = " and ".join(runner.svc) or "prod"
                return done("refused", f"needs about {need:.0f} GiB, {max(free, 0):.0f} GiB free now; turn on No {sv} "
                                       f"(Jobs) and measure once {sv} has stopped")
            if not runner._resume_job(name, reason="calibration"):
                return done("failed", "could not start or resume the job (see Events)")
    for c in range(cycles):
        cyc = {"warm": [], "parked": []}
        m["cycles"].append(cyc)
        # 2. warm
        m["phase"] = f"cycle {c + 1}: running"
        _progress_mark(m, sp, warm_s * c)
        t_end = time.time() + warm_s
        while time.time() < t_end:
            if interrupted():
                return done("interrupted", "a service needed the GPU; partial measurements kept")
            cyc["warm"].append(_sample(runner, name))
            time.sleep(poll_s)
        _progress_mark(m, sp, warm_s * (c + 1))
        # 3. park
        m["phase"] = f"cycle {c + 1}: parking"
        t0 = time.time()
        with runner.lock:
            runner._park_job(name, reason="calibration")
        cyc["park_s"] = round(time.time() - t0, 2)
        m["phase"] = f"cycle {c + 1}: parked"
        t_end = time.time() + settle_s
        while time.time() < t_end:
            cyc["parked"].append(_sample(runner, name))
            time.sleep(poll_s)
        if c == cycles - 1:
            break
        # 4. resume, timed until GPU memory is back to 90% of the steady level
        m["phase"] = f"cycle {c + 1}: resuming"
        steady = _steady(cyc["warm"])[0]
        t0 = time.time()
        with runner.lock:
            if runner.paused():
                return done("interrupted", "GapRunner was paused; partial measurements kept")
            if not runner._resume_job(name, reason="calibration"):
                return done("failed", "resume failed (see Events)")
        deadline = t0 + max(600.0, 5 * float(sp.get("resume_s") or 60))
        while time.time() < deadline and _sample(runner, name)[0] < 0.9 * steady:
            time.sleep(0.2)
        cyc["resume_s"] = round(time.time() - t0, 2)
    m["ssd_gib"] = _du_gib(sp.get("ssd_paths", [])) if sp.get("ssd_paths") else None
    return done("ok")


def _progress_mark(m, sp, run_s):
    from . import progress
    frac, text = progress.read(sp)
    if frac is not None:
        m["progress"].append([run_s, frac, text])


def _steady(samples):
    """(hbm, cpu) medians of the last half of the samples: the job's settled footprint."""
    tail = samples[len(samples) // 2:] or samples
    if not tail:
        return 0.0, 0.0
    return round(statistics.median(s[0] for s in tail), 1), round(statistics.median(s[1] for s in tail), 1)


def _finish(runner, name, m):
    warm = [s for c in m["cycles"] for s in c["warm"]]
    parked = [s for c in m["cycles"] for s in c["parked"]]
    parks = [c["park_s"] for c in m["cycles"] if "park_s" in c]
    resumes = [c["resume_s"] for c in m["cycles"] if "resume_s" in c]
    ssd = m.get("ssd_gib")
    hbm, cpu = _steady(warm)
    m["running"] = {"hbm_gib": hbm, "cpu_gib": cpu, "ssd_gib": ssd}
    m["running_peak"] = {"hbm_gib": max((s[0] for s in warm), default=0.0), "cpu_gib": max((s[1] for s in warm), default=0.0)}
    phbm, pcpu = _steady(parked)
    m["parked"] = {"hbm_gib": phbm, "cpu_gib": pcpu, "ssd_gib": ssd}
    m["park_s"] = round(statistics.median(parks), 2) if parks else None
    m["resume_s"] = round(statistics.median(resumes), 2) if resumes else None
    for s in parks:
        runner.costs.record(name, "park", s)
    for s in resumes:
        runner.costs.record(name, "resume", s)
    from . import progress
    per_h = progress.rate([(p[0], p[1]) for p in m["progress"]])
    last = m["progress"][-1][1] if m["progress"] else None
    m["progress_per_hour"] = round(per_h, 4) if per_h else None
    m["gpu_hours_left"] = progress.hours_left(last, per_h)
    m["gpu_hours_total"] = round(1 / per_h, 2) if per_h else None
    m["toml"] = snippet(name, m) if warm else None
    for c in m["cycles"]:                      # keep the file small: summaries, not every sample
        c["warm_n"], c["parked_n"] = len(c.pop("warm")), len(c.pop("parked"))
    os.makedirs(os.path.dirname(path(runner, name)), exist_ok=True)
    tmp = path(runner, name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1)
    os.replace(tmp, path(runner, name))
