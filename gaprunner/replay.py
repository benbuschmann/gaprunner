"""Replay a real request trace through the same scheduler rules, in simulated time (an idea taken from gpu-broker).

The gap model starts empty and learns as the trace plays, exactly as the daemon would: the projection is
out-of-sample, not fitted to the trace it is scored on. Compare policies with --p-min / --worth / --idle-park (p-min 0 = "start as soon as the service parks").

Trace formats
  audit   GapRunner's own audit.jsonl is not enough on its own (it logs transitions, not requests); use
          `requests`: JSON lines {"t": epoch, "event": "start"|"end", "service": name}
  yield   FREEZE/THAW logs: "<ISO>Z FREEZE ..." (request arrives) / "<ISO>Z THAW ... idle Ns" (idle since N s ago)

Model: one service (the first in the config). Parking a service is instant; a request that finds it parked waits
(park time of every running job + the service's wake time); a resumed job does useful work after its resume time.
"""
import datetime as dt
import json
import re
import statistics

from .gaps import GapModel
from .scheduler import HOT, PARKED, Job, Service, Snapshot, decide


def load_yield(paths, idle_cap=600, idle_default=5.0):
    """One segment per log file: time between files (no logging) is not counted as busy or idle."""
    return [seg for seg in (_yield_file(p, idle_cap, idle_default) for p in paths) if len(seg) >= 2]


def _yield_file(p, idle_cap, idle_default):
    ev = []
    with open(p) as f:
        for line in f:
            m = re.match(r"(\S+Z) (FREEZE|THAW)(.*)", line)
            if not m:
                continue
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
            if m.group(2) == "FREEZE":
                ev.append((t, "start"))
            else:
                n = re.search(r"idle (\d+)s", m.group(3))
                idle = float(n.group(1)) if n and float(n.group(1)) < idle_cap else idle_default
                ev.append((t - idle, "end"))
    ev.sort()
    out, busy = [], False                # keep the stream well-formed: start/end alternate
    for t, e in ev:
        if (e == "start") != busy:
            out.append((t, e))
            busy = e == "start"
    return out


def load_requests(paths, service=None):
    """One segment per file."""
    return [seg for seg in (_requests_file(p, service) for p in paths) if len(seg) >= 2]


def _requests_file(p, service):
    ev = []
    with open(p) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") in ("start", "end") and (service is None or r.get("service") == service):
                ev.append((float(r["t"]), r["event"]))
    return sorted(ev)


def simulate(cfg, segments, step_s=1.0, policy=None):
    """segments: list of event lists [(t, "start"|"end")]; the gap model carries over from one segment to the next."""
    policy = policy or cfg["policy"]
    sc = cfg["services"][0]
    model = GapModel(min_samples=20)
    total = cfg["gaprunner"]["gpu_total_gib"] or \
        sc["gpu_gib"] + max((j["gpu_gib"] for j in cfg["jobs"]), default=0) + policy.reserve_gib
    waits, job_s = [], {j["name"]: 0.0 for j in cfg["jobs"]}
    swaps = wakes = 0
    swap_s = covered = 0.0
    for events in segments:
        t0 = events[0][0]
        svc = Service(sc["name"], HOT, 0, t0, sc["gpu_gib"], sc["idle_park_s"], sc["wake_s"])
        jobs = [Job(j["name"], PARKED, j["gpu_gib"], j["park_s"], j["resume_s"], j["order"], j["hold"],
                    j["window_only"], j["enabled"], t0, None) for j in cfg["jobs"]]
        useful_from = {}
        i, now, end = 0, t0, events[-1][0]
        covered += end - t0
        first = True
        while now <= end:
            while i < len(events) and events[i][0] <= now:
                t, e = events[i]
                if e == "start":
                    if svc.inflight == 0 and not first:
                        model.add(t - svc.last_done)
                    if svc.state == PARKED:
                        hot = [j for j in jobs if j.state == HOT]
                        wait = sum(j.park_s for j in hot) + svc.wake_s
                        waits.append(wait)
                        swap_s += wait
                        wakes += 1
                        for j in hot:
                            j.state, j.parked_at, j.waiting_since = PARKED, t, t
                            useful_from.pop(j.name, None)
                        svc.state = HOT
                    svc.inflight = 1
                else:
                    svc.inflight, svc.last_done = 0, t
                first = False
                i += 1
            snap = Snapshot(now, [svc], jobs, total, None, False, False)
            for a in decide(snap, {svc.name: model}, policy):
                if a.kind == "park_service":
                    svc.state = PARKED
                elif a.kind == "resume_job":
                    j = next(x for x in jobs if x.name == a.target)
                    j.state, j.waiting_since = HOT, None
                    useful_from[j.name] = now + j.resume_s
                    swaps += 1
                    swap_s += j.resume_s
            for j in jobs:
                if j.state == HOT and now >= useful_from.get(j.name, now + 1):
                    job_s[j.name] += step_s
            now += step_s
    days = max(covered, 1.0) / 86400
    useful = sum(job_s.values())
    return {
        "trace_hours": round(covered / 3600, 1),
        "segments": len(segments),
        "gap_samples": len(model),
        "job_hours_per_day": {k: round(v / 3600 / days, 2) for k, v in job_s.items()},
        "total_job_hours_per_day": round(useful / 3600 / days, 2),
        "job_starts_per_day": round(swaps / days, 1),
        "service_wakes_per_day": round(wakes / days, 1),
        "switch_overhead_share": round(swap_s / (useful + swap_s), 3) if useful + swap_s else 0.0,
        "added_wait_s": {"p50": _pct(waits, 50), "p95": _pct(waits, 95), "max": round(max(waits), 2) if waits else 0.0},
        "requests_delayed_per_day": round(len(waits) / days, 1),
    }


def _pct(xs, q):
    if not xs:
        return 0.0
    if len(xs) == 1:
        return round(xs[0], 2)
    return round(statistics.quantiles(xs, n=100)[q - 1], 2)
