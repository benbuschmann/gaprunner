"""Is a job running and not broken? Is a service answering?

Checked every 10 s, report-only (nothing is killed or restarted automatically):
  progress   progress_cmd's value hasn't changed for stall_after_s of *running* time (default 30 min)
  log        the job's log hasn't been written for stall_after_s while the job ran (paused time doesn't count),
             and its progress isn't moving either (a job may log elsewhere)
  health     the recipe's health_cmd exits non-zero (checked every 60 s; ignored while the job starts up: until it
             first succeeds, or for health_grace_s of running time, default 900, whichever comes first)
  footprint  the job holds more than 1.5x its declared running GPU memory
  service    a service that should be up hasn't answered its driver for 30 s
An issue is raised at once and cleared after 3 clean checks in a row, so alerts don't flap. Raising and clearing
are written to the audit log and passed to notify_cmd (env GAPRUNNER_LEVEL, GAPRUNNER_TARGET, GAPRUNNER_MSG).
"""
import os
import subprocess

from .scheduler import HOT, STARTING, STOPPED, UNKNOWN

CHECK_EVERY_S = 10
CLEAR_AFTER = 3
FOOTPRINT_FACTOR = 1.5


def _issues_for_job(runner, name, j, now, holders):
    sp, out = j["spec"], []
    stall = float(sp.get("stall_after_s", 1800))
    if j["state"] != HOT:
        j.pop("health_ok_once", None)
        return out
    if j["paused"] or j.get("frozen"):
        return None                                              # paused or frozen: no verdict either way
    ran = j["run_s"] - j.get("hot_run_s", j["run_s"])          # running time since it last (re)started
    last_move = max(j.get("progress_changed_run_s", -1.0), j.get("part_changed_run_s", -1.0))
    moving = last_move >= 0 and j["run_s"] - last_move <= stall        # either progress line changed lately
    if sp.get("progress_cmd") and j["progress"] is not None and last_move >= 0 and not moving:
        out.append(f"no progress for {_ago(j['run_s'] - last_move)} of running")
    log = os.path.expanduser(sp.get("log_file") or "") or (
        os.path.join(runner.top["state_dir"], f"job-{_slug(name)}.log") if sp.get("start_cmd") else None)
    if log and os.path.exists(log) and ran > stall and not moving:    # a quiet log is fine while progress moves
        quiet = now - max(os.path.getmtime(log), j.get("active_since", now))
        if quiet > stall:
            out.append(f"log quiet for {_ago(quiet)}")
    if sp.get("health_cmd") and now - j.get("health_cmd_at", 0) >= 60:
        j["health_cmd_at"] = now
        try:
            r = subprocess.run(["sh", "-c", sp["health_cmd"]], capture_output=True, text=True, timeout=30,
                               cwd=os.path.expanduser(sp.get("workdir", ".")), env={**os.environ, **sp.get("env", {})})
            bad = None if r.returncode == 0 else \
                f"health check failed (exit {r.returncode}){': ' + (r.stderr or r.stdout).strip()[:120] if (r.stderr or r.stdout).strip() else ''}"
        except (OSError, subprocess.TimeoutExpired):
            bad = "health check timed out"
        if bad is None:
            j["health_ok_once"] = True
        starting = not j.get("health_ok_once") and ran < float(sp.get("health_grace_s", 900))
        j["health_cmd_bad"] = None if starting else bad
    if j.get("health_cmd_bad"):
        out.append(j["health_cmd_bad"])
    declared = float(sp["running"].get("hbm_gib") or 0)
    if declared and holders.get(name, 0) > FOOTPRINT_FACTOR * declared:
        out.append(f"using {holders[name]:.0f} GiB of GPU memory, more than 1.5x its declared {declared:.0f}")
    return out


def _issues_for_service(runner, name, s, now):
    if s["stopped"] or s["starting_since"] is not None or s.get("switching") or not runner.live:
        s.pop("unknown_since", None)
        return []
    if s["state"] == UNKNOWN:
        since = s.setdefault("unknown_since", now)
        if now - since >= 30:
            return [f"not answering for {now - since:.0f} s"]
    else:
        s.pop("unknown_since", None)
    return []


def check(runner):
    now = runner.clock()
    if now - runner.health_at < CHECK_EVERY_S:
        return
    runner.health_at = now
    holders, _, _ = runner.holders(runner.gpu.read())
    for kind, table, fn in (("job", runner.job, lambda n, e: _issues_for_job(runner, n, e, now, holders)),
                            ("service", runner.svc, lambda n, e: _issues_for_service(runner, n, e, now))):
        for n, e in table.items():
            issues = fn(n, e)
            if issues is None:                                   # no verdict this round (paused)
                continue
            if issues:
                e["clean_checks"] = 0
                if issues != e.get("health"):
                    if not e.get("health"):
                        runner.audit("unhealthy", target=n, kind=kind, issues=issues)
                        notify(runner, "warn", n, f"{n}: " + "; ".join(issues))
                    e["health"] = issues
            elif e.get("health"):
                e["clean_checks"] = e.get("clean_checks", 0) + 1
                if e["clean_checks"] >= CLEAR_AFTER:
                    runner.audit("healthy", target=n, kind=kind, was=e["health"])
                    notify(runner, "info", n, f"{n}: back to normal")
                    e["health"] = []


def write_notify_file(top, level, target, msg):
    path = top.get("notify_file")
    if not path:
        return
    import json
    import time
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "level": level, "target": target, "msg": msg}) + "\n")
    except OSError:
        pass


def _ago(seconds):
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


def notify(runner, level, target, msg):
    """Alerts go to notify_file (JSON lines a relay can forward) and/or notify_cmd (run with GAPRUNNER_* set)."""
    write_notify_file(runner.top, level, target, msg)
    cmd = runner.top.get("notify_cmd")
    if not cmd:
        return
    try:
        subprocess.Popen(["sh", "-c", cmd], env={**os.environ, "GAPRUNNER_LEVEL": level, "GAPRUNNER_TARGET": target,
                                                 "GAPRUNNER_MSG": msg},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _slug(name):
    from .runner import _slug as s
    return s(name)
