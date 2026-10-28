"""The daemon: keeps live state, runs the scheduler every tick, and owns every transition (one lock, one owner).

Modes
  shadow  Requests pass straight through. GapRunner learns each service's idle gaps and logs what it WOULD do.
  live    GapRunner parks and wakes services and parks and resumes jobs.

Safety rules (all from the design doc):
  - one lock around every transition; the gate's wake path and the tick loop never act at the same time;
  - memory counts as free only when the driver says so (wait_free) -- never on an engine's word;
  - a service wake parks every job first; a job that will not park is killed if kill_stuck_jobs, and if the driver
    still shows the memory in use the service is woken anyway and the overlap is logged (services first);
  - the pause file is a kill switch: wake services, park jobs, decide nothing;
  - every transition goes to the audit log (JSON lines) with its measured time.
"""
import collections
import json
import os
import subprocess
import threading
import time

from . import config as C
from . import drivers as D
from . import gpu as G
from .costs import CostBook
from .gaps import GapModel
from .scheduler import DONE, FAILED, HOT, PARKED, PARKING, STARTING, STOPPED, UNKNOWN, Job, Service, Snapshot, decide, explain, plan_wake

HISTORY_KEEP_S = 30 * 86400
ACTIVITY_KEEP_S = 7 * 86400


class Runner:
    def __init__(self, cfg, gpu=None, clock=time.time, readonly=False):
        self.cfg, self.top, self.policy = cfg, cfg["gaprunner"], cfg["policy"]
        self.clock, self.readonly = clock, readonly          # readonly: `gaprunner check`, which writes nothing
        if not readonly:
            os.makedirs(self.top["state_dir"], exist_ok=True)
        self.audit_path = os.path.join(self.top["state_dir"], "audit.jsonl")
        self.status_path = os.path.join(self.top["state_dir"], "status.json")
        self.gpu = gpu or G.NvidiaSmi(self.top["gpu_index"])
        self.lock = threading.RLock()
        self.costs = CostBook(os.path.join(self.top["state_dir"], "costs.json"))
        from . import servers
        self.finder, self.model_servers = servers.Finder(), []
        self.svc = {}
        for s in cfg["services"]:
            engines = {e["engine_name"]: {"spec": e, "driver": D.make(e)} for e in s["engines"]}
            for en, e in engines.items():
                key = s["name"] if len(engines) == 1 else f"{s['name']}/{en}"
                e["cost_key"] = key
                self.costs.seed(key, "park", e["spec"]["park_s"])
                self.costs.seed(key, "resume", e["spec"]["wake_s"])
            current = (_read_json(self._engine_path(s["name"])) or {}).get("engine")
            if current not in engines:
                current = s["default_engine"]
            self.svc[s["name"]] = {"service": s, "engines": engines, "engine": current,
                                   "spec": engines[current]["spec"], "driver": engines[current]["driver"],
                                   "cost_key": engines[current]["cost_key"], "state": UNKNOWN, "inflight": 0,
                                   "job_inflight": 0, "last_done": clock(), "gaps": GapModel.load(self._gap_path(s["name"])),
                                   "stopping": False, "stopped": False, "starting_since": None, "released": False,
                                   "switching": None, "schedule_slot": None, "dead_since": None, "wake_failures": 0}
        self.job = {}
        for j in cfg["jobs"]:
            self.job[j["name"]] = self._job_entry(j)
        from .jobsapi import Submissions
        self.submissions = Submissions(self)
        from .governor import Governor, GovernorJob
        govs = [e["driver"] for e in self.job.values() if isinstance(e["driver"], GovernorJob)]
        self.governor = Governor(self, govs) if govs else None
        self.count_lock = threading.Lock()
        self.last_plan = None
        self.recent = []
        self.calibrating = {}                 # job name -> live calibration progress
        self.health_at = 0.0
        self.claims = [{"spec": c, "driver": D.Driver({**c, "name": c["name"]}), "pids": [], "seen": 0.0}
                       for c in cfg.get("claims", [])]
        self.claims_at = 0.0
        self.settle_until = 0.0              # set by run_forever: no new starts while re-finding running jobs
        self.gpu_hold_path = os.path.join(self.top["state_dir"], "GPU_HOLD")
        self.drain_path = os.path.join(self.top["state_dir"], "DRAIN")
        self.host_mem = _mem_available_gib
        self.history_path = os.path.join(self.top["state_dir"], "history.jsonl")      # 1 row a minute, 30 days
        self.activity_path = os.path.join(self.top["state_dir"], "activity.jsonl")    # request start/end, 7 days
        self.live_rows = collections.deque(maxlen=720)                                 # 5 s rows, last hour
        self.activity = collections.deque(_tail_jsonl(self.activity_path, self.clock() - 86400))
        self.last_minute = 0
        self.submissions.load()               # jobs submitted through the Jobs API, back in the queue
        if not readonly:
            _trim_jsonl(self.history_path, self.clock() - HISTORY_KEEP_S)
            _trim_jsonl(self.activity_path, self.clock() - ACTIVITY_KEEP_S)
            _trim_jsonl(self.audit_path, self.clock() - HISTORY_KEEP_S)

    def _job_entry(self, j):
        """The runner's record of one job (from the config, or submitted through the Jobs API)."""
        clock = self.clock
        self.costs.seed(j["name"], "park", j["park_s"])
        self.costs.seed(j["name"], "resume", j["resume_s"])
        self.costs.seed(j["name"], "save", j.get("save_every_s"))   # learned from saved_cmd as it runs
        if j.get("start_cmd"):
            j["wrapper_pid_file"] = os.path.join(self.top["state_dir"], f"job-{_slug(j['name'])}.pid")
            if not (j.get("pid_file") or j.get("pids") or j.get("pgrep") or j.get("pids_cmd")):
                j["pid_file"] = j["wrapper_pid_file"]
                j["group"] = True          # GapRunner starts it in its own process group and signals the group
        return {"spec": j, "driver": D.make(j), "state": UNKNOWN, "waiting_since": clock(),
                "fallback": D.Restart(j) if j.get("fallback") else None,
                "parked_at": None, "proc": None, "paused": False,
                "run_s": 0.0, "progress": None, "progress_text": "", "prog": collections.deque(maxlen=60),
                "progress_at": 0.0, "save_point": None, "save_run_s": None, "save_checked": 0.0,
                "result": _read_json(self._result_path(j["name"])),
                "frozen": os.path.exists(self._frozen_path(j["name"]))}

    def add_job(self, spec, queue_at=None):
        """Add a job while running (the Jobs API). It is checked like a [[job]] entry; queue_at "top" or "bottom"
        places it in the queue file."""
        from .config import job_defaults
        with self.lock:
            if spec.get("name") in self.job or spec.get("name") in self.svc:
                raise ValueError(f"a job named {spec.get('name')!r} already exists")
            if _slug(str(spec.get("name"))) in {_slug(n) for n in self.job}:
                # its pid, result and log files would be another job's (names are cut to 60 characters for files)
                raise ValueError(f"a job's files would clash with another job's: choose a different name")
            if spec.get("driver") == "governor":
                raise ValueError("governor jobs come from the config")
            spec = job_defaults(spec, max((e["spec"]["order"] for e in self.job.values()), default=-1) + 1)
            entry = self._job_entry(spec)
            if spec.get("start_cmd") and not entry["driver"].pids() and entry["result"] is None:
                entry["state"] = PARKED           # waiting for its turn (refresh says the same from the next tick)
            self.job[spec["name"]] = entry
            self.cfg["jobs"].append(spec)
            if queue_at in ("top", "bottom") and not self.readonly:
                from .ui import ordered_queue
                q = [x for x in ordered_queue(self) if x["name"] != spec["name"]]
                row = {"name": spec["name"], "hold": bool(spec["hold"]), "window_only": bool(spec["window_only"]),
                       "added": round(self.clock(), 1), **({"tags": spec["tags"]} if spec.get("tags") else {})}
                q.insert(0 if queue_at == "top" else len(q), row)
                C.write_queue(self.top, {"jobs": q})
        return spec

    def remove_job(self, name):
        """Take a job out (the Jobs API's withdraw): stopped first if it holds anything, then forgotten."""
        with self.lock:                       # held throughout, so the scheduler can't start it in between
            if name not in self.job:
                raise ValueError("no such job")
            if self.job[name]["driver"].pids() or self.job[name]["proc"] is not None:
                self.stop_job(name)
            self.job.pop(name, None)
            self.cfg["jobs"] = [j for j in self.cfg["jobs"] if j["name"] != name]
            q = C.read_queue(self.top)
            q["jobs"] = [x for x in q.get("jobs", []) if x.get("name") != name]
            C.write_queue(self.top, q)

    def status_of(self, name):
        """{"word", "why"} for one job, as /api/status says it."""
        snap = self.snapshot()
        why = explain(snap, {n: e["gaps"] for n, e in self.svc.items()}, self.policy)
        word, text = why.get(name, (None, None))
        return {"word": word, "why": text}

    # ---------- bookkeeping ----------
    def _engine_path(self, name):
        return os.path.join(self.top["state_dir"], f"engine-{_slug(name)}.json")

    def _frozen_path(self, name):
        return os.path.join(self.top["state_dir"], f"job-{_slug(name)}.frozen")

    def freeze(self, name, on=True):
        """Freeze (SIGSTOP) a running job where it is, or thaw it. A frozen job keeps its memory and its place:
        GapRunner doesn't thaw it after service requests, doesn't judge its health, and doesn't park it."""
        j = self.job[name]
        with self.lock:
            if on:
                if j["state"] != HOT:
                    raise ValueError(f"{name} isn't running, so there is nothing to freeze")
                j["driver"].pause()
                j["frozen"] = True
                open(self._frozen_path(name), "w").close()
                self.audit("freeze", target=name)
                return f"Paused {name} where it is; it keeps its memory (Resume continues it)"
            j["driver"].unpause()
            j["frozen"], j["paused"] = False, False
            if os.path.exists(self._frozen_path(name)):
                os.remove(self._frozen_path(name))
            self.audit("thaw", target=name)
            return f"Resumed {name} from where it paused"

    def _set_hold(self, name, on):
        q = C.read_queue(self.top)
        if name not in [x.get("name") for x in q.get("jobs", [])]:
            q.setdefault("jobs", []).append({"name": name})
        for x in q["jobs"]:
            if x.get("name") == name:
                x["hold"] = on
        C.write_queue(self.top, q)

    def coexist_of(self, name):
        """The job's placement, with a change made on the page (queue override) winning over the recipe."""
        return C.queue_overrides(self.top).get(name, {}).get("coexist") or self.job[name]["spec"]["coexist"]

    def stop_job(self, name):
        """Stop: end the job now (SIGTERM to its processes, SIGKILL after 15 s) and wind it down, so it doesn't start
        again until someone presses Start. A resumable job continues from its last checkpoint then."""
        import signal as sig
        j = self.job[name]
        q = C.read_queue(self.top)
        if name not in [x.get("name") for x in q.get("jobs", [])]:
            q.setdefault("jobs", []).append({"name": name})
        for x in q["jobs"]:
            if x.get("name") == name:
                x["hold"] = True
        C.write_queue(self.top, q)
        with self.lock:
            pids = j["driver"].pids()
            if not pids:
                return f"{name} wasn't running; it's wound down until you press Start"
            if j["frozen"] or j["paused"]:
                j["driver"].unpause()            # a stopped (SIGSTOP) process can't act on SIGTERM
                j["frozen"] = j["paused"] = False
                if os.path.exists(self._frozen_path(name)):
                    os.remove(self._frozen_path(name))
            j["expect_exit"] = True
            j["driver"].signal(sig.SIGTERM)
            deadline = time.monotonic() + 15
            while any(D.alive(p) for p in pids) and time.monotonic() < deadline:
                time.sleep(0.2)
            if any(D.alive(p) for p in pids):
                j["driver"].signal(sig.SIGKILL)
                for p in pids:                    # a child left behind when its group leader already exited
                    if D.alive(p):
                        try:
                            os.kill(p, sig.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
            self._check_exits()
            j["state"], j["parked_at"] = PARKED, self.clock()
        self.audit("stop", target=name)
        return f"Stopped {name}; it's wound down until you press Start"

    def clear_result(self, name):
        """Forget a finished job's exit (Start again), so the scheduler may start it once more."""
        try:
            os.remove(self._result_path(name))
        except FileNotFoundError:
            pass
        self.job[name]["result"] = None
        self.audit("result_cleared", target=name)

    def _result_path(self, name):
        return os.path.join(self.top["state_dir"], f"job-{_slug(name)}.result.json")

    def _gap_path(self, name):
        return os.path.join(self.top["state_dir"], f"gaps-{name}.json")

    def audit(self, event, **kw):
        row = {"t": round(self.clock(), 3), "event": event, **kw}
        if self.readonly:
            return
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        self.recent = (self.recent + [row])[-50:]

    @property
    def live(self):
        return self.top["mode"] == "live"

    def paused(self):
        return os.path.exists(self.top["pause_file"])

    # ---------- requests (called by the gate) ----------
    def request_start(self, name, job=None):
        s = self.svc[name]
        if job is not None:                   # a job's own requests (benchmarks against the service)
            with self.count_lock:
                s["job_inflight"] += 1
            return
        with self.count_lock:
            first = not any(e["inflight"] for e in self.svc.values())
            if s["inflight"] == 0:
                gap = self.clock() - s["last_done"]
                if gap > 0:
                    s["gaps"].add(gap)
                self._activity(name, "start")
            s["inflight"] += 1
        if first and self.live:
            for jn, j in self.job.items():
                if self.coexist_of(jn) == "yield" and j["state"] == HOT and not j["paused"] and not j["frozen"]:
                    j["driver"].pause()
                    j["paused"] = True
                    j["gave_way"] = j.get("gave_way", 0) + 1
                    self.audit("yield_pause", target=jn, reason=f"{name} got a request")

    def request_end(self, name, job=None):
        s = self.svc[name]
        if job is not None:
            with self.count_lock:
                s["job_inflight"] -= 1
                s["job_last_done"] = self.clock()
            return
        with self.count_lock:
            s["inflight"] -= 1
            s["last_done"] = self.clock()
            if s["inflight"] == 0:
                self._activity(name, "end")

    def _activity(self, name, event):
        row = {"t": round(self.clock(), 3), "event": event, "service": name}
        self.activity.append(row)
        while self.activity and self.activity[0]["t"] < row["t"] - 86400:
            self.activity.popleft()
        with open(self.activity_path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def ensure_hot(self, name):
        """Make a service ready for a request. True when it is (or, in shadow mode, assumed to be) serving."""
        s = self.svc[name]
        if self.unavailable(name):
            return False
        # request_start has already counted this request; the tick sets PARKING before it re-checks inflight,
        # so either we see PARKING/PARKED here and wait for the lock, or the tick sees our request and skips the park
        if not self.live or s["state"] == HOT:
            return True
        with self.lock:
            if s["state"] == HOT:
                return True
            t0 = self.clock()
            # a deadline for the whole wake: jobs that haven't stepped aside by then are stopped hard (see _park_job)
            end = time.monotonic() + (self.policy.wake_deadline_s or 3 * self.policy.max_added_wait_s)
            left = lambda: max(0.0, end - time.monotonic())
            need = s["spec"]["gpu_gib"] - float(s["spec"]["parked"]["hbm_gib"] or 0)   # it keeps its parked residue
            for jn in plan_wake(self.snapshot(), name, self.policy.reserve_gib):
                self._park_job(jn, reason=f"{name} requested", cap=left())
            if not G.wait_free(self.gpu, need, min(self.top["verify_free_s"], max(2.0, left())), self.policy.reserve_gib):
                rest = plan_wake(self.snapshot())               # not enough: park the jobs kept beside it as well
                for jn in rest:
                    self._park_job(jn, reason=f"{name} requested; memory still short", cap=left())
                if not (rest and G.wait_free(self.gpu, need, min(self.top["verify_free_s"], max(2.0, left())), self.policy.reserve_gib)):
                    self.audit("overlap_risk", service=name,
                               note="memory still in use by something GapRunner does not control; waking anyway")
            ok = self._transition(s, name, "resume")
            total = round(self.clock() - t0, 3)
            self.audit("request_wake", service=name, ok=ok, total_s=total,
                       **({"over_budget_s": round(total - self.policy.max_added_wait_s, 3)} if total > self.policy.max_added_wait_s else {}))
            if ok:
                s["dead_since"], s["wake_failures"] = None, 0
            else:
                # requests fail fast from now on instead of each waiting out another wake; the tick restarts it
                s["dead_since"], s["wake_failures"] = self.clock(), s["wake_failures"] + 1
                from . import health
                health.notify(self, "warn", name, f"{name} failed to wake for a request"
                              + (": restarting it" if self._restartable(name) else "; requests get 503 until it answers"))
            return ok

    def _restartable(self, name):
        s = self.svc[name]
        return bool(s["spec"].get("stop_cmd") and s["spec"].get("start_cmd") and s["spec"]["restart_on_failed_wake"])

    def _recover(self):
        """A service that failed to wake: restart it (stop_cmd, then start_cmd) when it can be, the way the end of a
        no-service period starts it again; otherwise let requests try to wake it again after dead_retry_s."""
        for n, s in self.svc.items():
            if s["dead_since"] is None or s["stopped"] or s["stopping"] or s["starting_since"] is not None:
                continue
            sp = s["spec"]
            if not self._restartable(n):
                if self.clock() - s["dead_since"] >= sp.get("dead_retry_s", 60):
                    s["dead_since"] = None                     # the next request tries a wake again
                continue
            for jn in plan_wake(self.snapshot(), n, self.policy.reserve_gib):
                self._park_job(jn, reason=f"{n} is restarting after a failed wake")
            self.audit("service_restart", target=n, reason=f"failed to wake ({s.get('last_error') or 'no answer'})")
            self._shell(sp["stop_cmd"], sp.get("stop_timeout_s", 300), n, "stop")
            if self._shell(sp["start_cmd"], sp.get("start_timeout_s", 300), n, "start"):
                s["starting_since"], s["state"] = self.clock(), STARTING
            s["dead_since"] = None

    def switch_engine(self, name, target, reason="by hand"):
        """Switch a service to another engine in a background thread. The old engine is stopped (its stop_cmd)
        unless it says switch_out = "sleep"; jobs that don't fit beside the new engine are parked; the new engine is
        woken if asleep, else started (start_cmd) and waited for. Requests meanwhile get a 503 saying why."""
        s = self.svc[name]
        if target not in s["engines"]:
            raise ValueError(f"{name} has no engine {target!r}")
        if s["switching"]:
            raise ValueError(f"{name} is already switching to {s['switching']['to']}")
        if target == s["engine"]:
            return f"{name} already runs {target}"
        s["switching"] = {"to": target, "since": self.clock(), "reason": reason}
        threading.Thread(target=self._switch, args=(name, target, reason), daemon=True, name=f"switch-{name}").start()
        return f"Switching {name} from {s['engine']} to {target}"

    def _switch(self, name, target, reason):
        s = self.svc[name]
        old, t0 = s["engine"], self.clock()
        oe, ne = s["engines"][old], s["engines"][target]
        self.audit("engine_switch_start", target=name, frm=old, to=target, reason=reason)
        try:
            with self.lock:
                need = float(ne["spec"]["running"]["hbm_gib"] or 0)
                for jn in plan_wake(self._snapshot_with(name, need), name, self.policy.reserve_gib):
                    self._park_job(jn, reason=f"{name} switching to {target}")
                deadline = self.clock() + float(s["service"].get("switch_drain_s", 120))
            while (s["inflight"] or s["job_inflight"]) and self.clock() < deadline:
                time.sleep(0.2)                  # requests already accepted finish first; new ones get the 503
            with self.lock:
                if oe["spec"].get("switch_out") == "sleep" and oe["driver"].state() == HOT:
                    oe["driver"].park()
                else:
                    if not self._shell(oe["spec"]["stop_cmd"], oe["spec"].get("stop_timeout_s", 300), name, "stop"):
                        raise RuntimeError(f"could not stop {old}")
                s["released"] = False
            if not G.wait_free(self.gpu, need - float(ne["spec"]["parked"]["hbm_gib"] or 0),
                               self.top["verify_free_s"], self.policy.reserve_gib):
                raise RuntimeError(f"not enough free GPU memory to start {target}")
            if ne["driver"].state() == PARKED:
                ne["driver"].resume()
            elif ne["driver"].state() != HOT:
                if not self._shell(ne["spec"]["start_cmd"], ne["spec"].get("start_timeout_s", 300), name, "start"):
                    raise RuntimeError(f"could not start {target}")
                deadline = self.clock() + float(ne["spec"]["ready_timeout_s"])
                while ne["driver"].state() != HOT:
                    if self.clock() > deadline:
                        raise RuntimeError(f"{target} not ready after {ne['spec']['ready_timeout_s']} s")
                    time.sleep(1)
            with self.lock:
                s.update(engine=target, spec=ne["spec"], driver=ne["driver"], cost_key=ne["cost_key"], state=HOT,
                         last_done=self.clock())
                with open(self._engine_path(name), "w") as f:
                    json.dump({"engine": target, "t": round(self.clock(), 1)}, f)
            self._warm(name)
            secs = round(self.clock() - t0, 1)
            self.audit("engine_switch", target=name, frm=old, to=target, seconds=secs, reason=reason)
            from . import health
            health.notify(self, "info", name, f"{name} now runs {target} (switched from {old} in {secs:.0f} s)")
        except Exception as e:  # noqa: BLE001 -- report, then try to bring the old engine back
            self.audit("engine_switch_failed", target=name, frm=old, to=target, error=str(e)[:300])
            from . import health
            health.notify(self, "warn", name, f"{name}: switch to {target} failed ({e}); restoring {old}")
            # it may still be loading and holding memory: always stop it first (stopping a stopped engine is harmless)
            self._shell(ne["spec"]["stop_cmd"], ne["spec"].get("stop_timeout_s", 300), name, "stop")
            G.wait_free(self.gpu, float(oe["spec"]["running"]["hbm_gib"] or 0) - float(oe["spec"]["parked"]["hbm_gib"] or 0),
                        self.top["verify_free_s"], self.policy.reserve_gib)
            if oe["driver"].state() == PARKED:
                oe["driver"].resume()
            elif oe["driver"].state() != HOT:
                self._shell(oe["spec"]["start_cmd"], oe["spec"].get("start_timeout_s", 300), name, "start")
                s["starting_since"] = self.clock()     # the window logic waits for it and reports when ready
        finally:
            s["switching"] = None

    def _snapshot_with(self, name, need_gib):
        snap = self.snapshot()
        for sv in snap.services:
            if sv.name == name:
                sv.gpu_gib = need_gib
        return snap

    def _schedules(self):
        """Daily engine schedules: at each slot's time, switch once; a switch by hand holds until the next slot."""
        now = time.localtime(self.clock())
        today, minute = time.strftime("%Y-%m-%d", now), now.tm_hour * 60 + now.tm_min
        for n, s in self.svc.items():
            slots = sorted(s["service"].get("schedule", []), key=lambda x: x["at"])
            if not slots or s["switching"]:
                continue
            due = [x for x in slots if int(x["at"][:2]) * 60 + int(x["at"][3:5]) <= minute]
            yesterday = time.strftime("%Y-%m-%d", time.localtime(self.clock() - 86400))
            slot, day = (due[-1], today) if due else (slots[-1], yesterday)     # before the first slot: last night's
            key = f"{day} {slot['at']}"
            if s["schedule_slot"] is None:
                s["schedule_slot"] = key               # don't act on a slot that began before GapRunner started
                continue
            if key != s["schedule_slot"]:
                s["schedule_slot"] = key
                if slot["engine"] != s["engine"]:
                    self.switch_engine(n, slot["engine"], reason=f"schedule {slot['at']}")

    def unavailable(self, name):
        """Why a service can't take requests right now (an exclusive window, an engine switch), or None."""
        s = self.svc[name]
        if s["switching"]:
            return f"{name} is switching to {s['switching']['to']} (since {time.strftime('%H:%M', time.localtime(s['switching']['since']))})"
        if not self.live:
            return None
        if s["stopping"] or s["stopped"]:
            w = C.window_until(self.top)
            return f"{name} is off (no {name}) so jobs can use the whole GPU" + (f", until {time.strftime('%H:%M', time.localtime(w))}" if w else "")
        if s["starting_since"] is not None:
            return f"{name} is starting again (since {time.strftime('%H:%M', time.localtime(s['starting_since']))})"
        if s["dead_since"] is not None:
            return (f"{name} failed to wake at {time.strftime('%H:%M', time.localtime(s['dead_since']))}"
                    + ("; it is being restarted" if self._restartable(name) else "; it will be tried again shortly"))
        return None

    def _windows(self):
        """Exclusive windows: stop services that have a stop_cmd when one opens, start them when it ends."""
        ex = C.exclusive_window(self.top)
        for n, s in self.svc.items():
            sp = s["spec"]
            if not sp.get("stop_cmd"):
                continue
            if ex and not s["stopped"]:
                s["stopping"] = True                     # new requests get 503 from now on
                if s["inflight"] and self.clock() - s.get("stop_asked", self.clock()) < sp.get("stop_grace_s", 60):
                    s.setdefault("stop_asked", self.clock())
                    continue                             # let requests in flight finish first
                ok = self._shell(sp["stop_cmd"], sp.get("stop_timeout_s", 300), n, "stop")
                s.pop("stop_asked", None)
                if ok:
                    s["stopped"], s["stopping"], s["state"] = True, False, STOPPED
            elif not ex and (s["stopped"] or s["stopping"]):
                for jn in plan_wake(self.snapshot(), n, self.policy.reserve_gib):
                    self._park_job(jn, reason="GPU window ended")
                if s["stopped"] and self._shell(sp["start_cmd"], sp.get("start_timeout_s", 300), n, "start"):
                    s["starting_since"], s["state"] = self.clock(), STARTING
                s["stopped"] = s["stopping"] = False
            if s["starting_since"] is not None:
                if s["driver"].state() == HOT:
                    self.audit("service_ready", target=n, seconds=round(self.clock() - s["starting_since"], 1))
                    s["starting_since"], s["state"], s["last_done"] = None, HOT, self.clock()
                    self._warm(n)
                elif self.clock() - s["starting_since"] > sp.get("ready_timeout_s", 1800):
                    self.audit("service_start_timeout", target=n, note="still not ready; check it by hand")
                    s["starting_since"] = None

    def _release_if_short(self, snap):
        """If an eligible job is waiting only because host memory is short, ask hot services that can (release_cmd)
        to give back cached host memory, e.g. vLLM's pinned sleep buffer. Once per wake; the next park is slower."""
        short = [j for j in snap.jobs if j.state == PARKED and not j.hold and not j.blocked_by and snap.cpu_free_gib is not None
                 and snap.cpu_free_gib - j.run_cpu_gib < self.policy.cpu_floor_gib]
        if not short:
            return
        for n, s in self.svc.items():
            if s["spec"].get("release_cmd") and s["state"] == HOT and not s["released"]:
                if self._shell(s["spec"]["release_cmd"], 120, n, "release"):
                    s["released"] = True

    def holds(self):
        """Why nothing new may start right now, or None: drain, a claim's process running, a GPU hold, or settling."""
        if os.path.exists(self.drain_path):
            return "draining: running jobs finish, nothing new starts"
        gh = _read_json(self.gpu_hold_path)
        if gh:
            return f"GPU hold: {gh.get('job')} was killed and processes {gh.get('pids')} are still alive"
        for c in self.claims:
            if c["spec"]["action"] == "hold" and (c["pids"] or self.clock() - c["seen"] < c["spec"]["linger_s"]):
                return f"claim {c['spec']['name']}: hand-run work is using the GPU"
        if self.clock() < self.settle_until:
            return "just started: re-finding running jobs"
        for n, s in self.svc.items():
            if s["switching"]:
                return f"{n} is switching to {s['switching']['to']}: nothing new starts until it's done"
        return None

    def _claims_and_holds(self):
        now = self.clock()
        if now - self.claims_at >= 2:
            self.claims_at = now
            for c in self.claims:
                c["pids"] = c["driver"].pids()
                if c["pids"]:
                    c["seen"] = now
        gh = _read_json(self.gpu_hold_path)
        if gh and not any(D.alive(p) for p in gh.get("pids", [])):
            os.remove(self.gpu_hold_path)
            self.audit("gpu_hold_cleared", target=gh.get("job"), note="the killed processes are gone")

    def tags_of(self, name):
        """Tags set in the UI, else the recipe's `tags`."""
        t = C.queue_overrides(self.top).get(name, {}).get("tags")
        return list(self.job[name]["spec"].get("tags", []) if t is None else t)

    def after_of(self, name):
        """The jobs this one waits for: set in the UI (Queue), else the recipe's `after`."""
        a = C.queue_overrides(self.top).get(name, {}).get("after")
        return [d for d in (self.job[name]["spec"].get("after", []) if a is None else a) if d in self.job]

    def _blocked_by(self, j, name):
        want, svc = j["spec"].get("requires_engine"), j["spec"].get("target_service")
        if want and self.svc[svc]["engine"] != want:
            return f"waiting for {svc} to run {want} (it runs {self.svc[svc]['engine']})"
        for dep in self.after_of(name):
            r = (self.job[dep]["result"] or {}).get("rc")
            if r is None:
                return f"waiting for {dep} to finish"
            if r != 0:
                return f"{dep} failed (exit {r}); fix it, or remove it from after"
        return None

    def _track_progress(self):
        """Count each job's running time, and read its progress_cmd every progress_every_s (30 s by default; or once
        after it stops)."""
        from . import progress
        now = self.clock()
        q = C.queue_overrides(self.top)
        for n, j in self.job.items():
            running = j["state"] == HOT and not j["paused"] and not j["frozen"]
            held = (q[n]["hold"] if n in q else bool(j["spec"].get("hold"))) or os.path.exists(self.drain_path)   # Wind down all: like Wind down on each
            self._wind_down_at_saved_point(n, j, held, now)
            if running:
                j["run_s"] += now - j.get("last_tick", now)
                if not j.get("was_running"):
                    j["active_since"], j["hot_run_s"] = now, j["run_s"]     # (re)started or un-paused
            j["was_running"] = running
            j["last_tick"] = now
            self._track_saves(n, j, running, now)
            cap = j["spec"].get("max_hours")
            if cap and running and j["run_s"] - j.get("cap_base", 0.0) >= float(cap) * 3600:
                q = C.read_queue(self.top)
                names = [x.get("name") for x in q.get("jobs", [])]
                if n not in names:
                    q.setdefault("jobs", []).append({"name": n})
                for x in q["jobs"]:
                    if x.get("name") == n:
                        x["hold"] = True
                C.write_queue(self.top, q)
                with self.lock:
                    self._park_job(n, reason=f"reached max_hours = {cap}")
                j["cap_base"] = j["run_s"]
                self.audit("max_hours", target=n, note=f"ran {cap} h; parked and held; release it to give it another {cap} h")
                from . import health
                health.notify(self, "warn", n, f"{n}: reached its {cap} h limit; parked and held")
            if j["spec"].get("summary_cmd") and (running or j.get("summary") is None) and now - j.get("summary_at", 0.0) >= 60:
                j["summary_at"] = now
                got = progress.summary(j["spec"])
                if got is not None:
                    j["summary"] = got
            if j["spec"].get("progress_cmd") and now - j["progress_at"] >= max(1.0, float(j["spec"].get("progress_every_s", 30))):
                j["progress_at"] = now
                frac, text, sub, sub_text = progress.read_both(j["spec"])
                if (sub, sub_text) != (j.get("part"), j.get("part_text")) and (sub is not None or sub_text):
                    j["part_changed_run_s"] = j["run_s"]           # the second line moving counts as progress too
                j["part"], j["part_text"] = sub, sub_text
                if frac is not None:
                    if frac != j["progress"]:
                        j["progress_changed_run_s"] = j["run_s"]
                    j["progress"], j["progress_text"] = frac, text
                    j["prog"].append((j["run_s"], frac))

    def _track_saves(self, n, j, running, now):
        """Learn how much running time a job that loses work needs between saved points (saved_cmd, checked once a
        minute while it runs). The scheduler uses it to give such a job only quiet spells long enough to save."""
        if not (j["spec"]["loses_work"] and j["spec"].get("saved_cmd") and running) or now - j["save_checked"] < 60:
            return
        from . import progress
        j["save_checked"] = now
        v = progress.saved_point(j["spec"])
        if v is None or v == j["save_point"]:
            return
        first, j["save_point"] = j["save_point"] is None, v
        if first:
            return                     # where it is, not when it got there (unless it was started from a saved point)
        if j["save_run_s"] is not None:
            self.costs.record(n, "save", j["run_s"] - j["save_run_s"])
        j["save_run_s"] = j["run_s"]

    def unsaved_s(self, n):
        """Running time since a work-losing job's last saved point (what stepping aside now would lose), or None."""
        j = self.job[n]
        if not j["spec"]["loses_work"] or j["save_run_s"] is None or j["state"] != HOT:
            return None
        return max(0.0, j["run_s"] - j["save_run_s"])

    def _wind_down_at_saved_point(self, n, j, held, now):
        """A wound-down job with saved_cmd stops as soon as its saved point moves on (it then resumes from there).
        Without saved_cmd, wind down lets the job finish its run."""
        from . import progress
        if not j["spec"].get("saved_cmd") or j["state"] != HOT or not held:
            j["saved_base"] = None
            return
        if j["frozen"] or now - j.get("saved_at", 0.0) < 15:
            return
        j["saved_at"] = now
        v = progress.saved_point(j["spec"])
        if v is None:
            return
        if j.get("saved_base") is None:
            j["saved_base"] = v                       # where it was when winding down started
        elif v != j["saved_base"]:
            with self.lock:
                self._park_job(n, reason=f"wound down: stopped at its saved point {v}")
            self.audit("wound_down", target=n, note=f"stopped at its saved point {v}; Start or Queue continues from there")
            j["saved_base"] = None

    def _shell(self, cmd, timeout, name, what):
        t0 = self.clock()
        try:
            r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
            ok = r.returncode == 0
            self.audit(f"service_{what}", target=name, ok=ok, seconds=round(self.clock() - t0, 1),
                       error=None if ok else (r.stderr or r.stdout).strip()[:300])
            return ok
        except subprocess.TimeoutExpired:
            self.audit(f"service_{what}", target=name, ok=False, error=f"timed out after {timeout}s")
            return False

    # ---------- transitions ----------
    def _transition(self, entry, name, op, reason=""):
        d, t0 = entry["driver"], self.clock()
        host0 = self.host_mem() if op == "park" else None
        try:
            (d.park if op == "park" else d.resume)()
        except Exception as e:  # noqa: BLE001 -- any driver failure is logged and reported, never raised into the gate
            self.audit(f"{op}_failed", target=name, error=str(e)[:300], reason=reason)
            entry["last_error"] = str(e)[:300]
            entry["state"] = d.state()
            return False
        dt_ = self.clock() - t0
        self.costs.record(entry.get("cost_key", name), op, dt_)
        entry["state"] = PARKED if op == "park" else HOT
        took = None
        if host0 is not None and self.live:
            host1 = self.host_mem()
            if host1 is not None:          # host memory the park took (weights or a frozen copy moving to RAM)
                took = round(max(0.0, host0 - host1), 1)
                if took >= 1.0:            # a park that stopped the process keeps nothing: it says nothing about
                    self.costs.record(entry.get("cost_key", name), "park_host", took)     # what a copy costs
        self.audit(op, target=name, seconds=round(dt_, 3), reason=reason,
                   **({"host_gib": took} if took is not None else {}))
        if op == "resume" and name in self.svc:
            self._warm(name)
        return True

    def _warm(self, name):
        """Run the engine's warm_cmd in the background after it wakes or starts (e.g. read lookup tables back into the
        page cache), so it reaches full speed sooner. Requests are answered meanwhile; the page shows it as warming up."""
        s = self.svc[name]
        cmd = s["spec"].get("warm_cmd")
        if not cmd or not self.live or (s.get("warm_proc") and s["warm_proc"].poll() is None):
            return
        log = open(os.path.join(self.top["state_dir"], f"service-{name}-warm.log"), "a")
        s["warm_proc"] = p = subprocess.Popen(["sh", "-c", cmd], stdout=log, stderr=subprocess.STDOUT,
                                              start_new_session=True)
        s["warm_since"] = t0 = self.clock()
        self.audit("warm_start", target=name, engine=s["engine"])

        def wait():
            rc = p.wait()
            log.close()
            s["warm_since"] = None
            self.audit("warm_done", target=name, ok=rc == 0, rc=rc, seconds=round(self.clock() - t0, 1))
        threading.Thread(target=wait, daemon=True, name=f"warm-{name}").start()

    def _park_job(self, name, reason="", cap=None):
        """Step a job aside. cap: seconds left before a waiting request's deadline; the job's own timeout is cut to
        max(cap, twice its measured step-aside time, 1 s), so a stuck job is stopped hard soon instead of after timeout_s."""
        j = self.job[name]
        j["expect_exit"] = True               # a job that exits while being parked has not failed
        if j["paused"] or j["frozen"]:
            j["driver"].unpause()             # a SIGSTOPped process can't checkpoint or handle SIGTERM
            j["paused"] = False
        if j["frozen"]:                       # someone paused it on purpose: keep it off after this step-aside
            j["frozen"] = False
            if os.path.exists(self._frozen_path(name)):
                os.remove(self._frozen_path(name))
            self._set_hold(name, True)
            self.audit("wound_down", target=name, reason="it was paused by hand and had to step aside for a service")
        extra = self.host_park_gib(name, float(j["spec"]["parked"]["cpu_gib"] or 0) -
                                   float(j["spec"]["running"]["cpu_gib"] or 0))
        free = self.host_mem()
        if free is not None and extra > 0 and free - extra < self.policy.cpu_floor_gib:
            # its frozen copy doesn't fit: first stop frozen jobs lower in the queue, last first
            if self._evict_for_host(name, self.policy.cpu_floor_gib - (free - extra)):
                free = self.host_mem()
        drivers_ = [d for d in (j["driver"], j["fallback"]) if d is not None]
        saved = [d.timeout for d in drivers_]
        if cap is not None:
            est = float(self.costs.get(name, "park")[0] or 0)
            for d in drivers_:
                d.timeout = min(d.timeout, max(cap, 2 * est, 1.0))
        try:
            if j["fallback"] and free is not None and extra > 0 and free - extra < self.policy.cpu_floor_gib:
                # not enough host RAM to hold its frozen copy: stop it instead; it resumes from its own checkpoint
                t0 = self.clock()
                try:
                    j["fallback"].park()
                    ok = True
                except Exception as e:  # noqa: BLE001 -- reported like any failed park
                    self.audit("park_failed", target=name, error=str(e)[:300], reason=reason)
                    ok = False
                if ok:
                    self.audit("park", target=name, seconds=round(self.clock() - t0, 3), how="stopped",
                               reason=f"{reason}; not enough host RAM to sleep ({free:.0f} GiB free, needs {extra:.0f})")
            else:
                lost = self.unsaved_s(name)
                ok = self._transition(j, name, "park", reason)
                if ok and lost:
                    self.audit("lost_work", target=name, seconds=round(lost, 1),
                               note="running time since its last saved point; it resumes from that point")
        finally:
            for d, t in zip(drivers_, saved):
                d.timeout = t
        if j["spec"]["loses_work"]:
            j["save_run_s"] = j["run_s"]          # the next start begins at the last saved point
        self._check_exits()
        if not ok and self.top["kill_stuck_jobs"]:
            pids = j["driver"].pids()
            j["driver"].kill()
            j["state"] = PARKED
            self.audit("job_killed", target=name, reason="would not park; services first")
            from . import health
            health.notify(self, "warn", name, f"{name}: would not park, so it was killed")
            deadline = time.monotonic() + 10
            while any(D.alive(p) for p in pids) and time.monotonic() < deadline:
                time.sleep(0.2)
            left = [p for p in pids if D.alive(p)]
            if left:                              # its memory may never come back: start nothing until it does
                with open(self.gpu_hold_path, "w") as f:
                    json.dump({"job": name, "pids": left, "t": round(self.clock(), 1)}, f)
                self.audit("gpu_hold", target=name, pids=left, note="killed processes still alive; no new starts")
                from . import health
                health.notify(self, "warn", name, f"GPU hold: {name} was killed but its processes are still alive")
        self._close_gpu_peak(name)
        j["parked_at"] = self.clock()
        j["waiting_since"] = self.clock()
        return ok

    def host_park_gib(self, key, declared):
        """Host memory a park takes: the recipe's figure, or what GapRunner measured (MemAvailable before and after
        its recent parks, median) when that is larger. A recipe that understates it would let host memory run out."""
        measured, ok = self.costs.get(key, "park_host")
        return max(float(declared or 0), float(measured or 0)) if ok else float(declared or 0)

    def gpu_peak_gib(self, name):
        """The job's measured GPU peak per run (median of its recent runs' peaks), or None before one is seen."""
        v, ok = self.costs.get(name, "gpu_peak")
        return v if ok else None

    def _close_gpu_peak(self, name):
        peak = self.job[name].pop("gpu_peak_now", None)
        if peak:
            self.costs.record(name, "gpu_peak", peak)

    def _evict_for_host(self, name, need_gib):
        """Make room in host memory for `name` to step aside by freezing: stop jobs that sit parked with their state
        held in host memory (frozen or asleep) and are lower in the queue than `name`, last in the queue first, until
        about need_gib is freed. A stopped job starts again later from its own checkpoint (start_cmd), so it loses
        only the work since its last saved point. Returns the GiB freed (from the stopped processes' RSS)."""
        q = C.queue_overrides(self.top)
        order = lambda n: q.get(n, {}).get("order", self.job[n]["spec"].get("order", 0))
        mine = order(name)
        held = [n for n, e in self.job.items() if n != name and e["state"] == PARKED and order(n) > mine
                and e["spec"].get("start_cmd") and e["driver"].pids()]
        freed = 0.0
        for n in sorted(held, key=order, reverse=True):
            if freed >= need_gib:
                break
            e = self.job[n]
            pids = e["driver"].pids()
            gib = _rss_gib(pids) or float(e["spec"]["parked"]["cpu_gib"] or 0)
            e["expect_exit"] = True               # stopped on purpose: not a failure
            try:
                (e["fallback"] or D.Restart(e["spec"])).park()
            except Exception as ex:  # noqa: BLE001 -- reported, and the next candidate is tried
                self.audit("evict_failed", target=n, error=str(ex)[:300], reason=f"host memory for {name}")
                continue
            freed += gib
            self.audit("evicted", target=n, gib=round(gib, 1),
                       reason=f"host memory for {name}, higher in the queue; it starts again from its own checkpoint")
        return freed

    def _resume_job(self, name, reason=""):
        j = self.job[name]
        if not G.wait_free(self.gpu, j["spec"]["gpu_gib"], self.top["verify_free_s"], self.policy.reserve_gib):
            self.audit("resume_skipped", target=name, reason="driver does not show enough free memory")
            return False
        if j["proc"] is not None:                 # our last launch hasn't been collected: never launch twice
            if j["proc"].poll() is None:
                if j["driver"].state() != PARKED:
                    return True                   # still running
                ok = self._transition(j, name, "resume", reason)   # launched and asleep: wake it
                if ok:
                    j["waiting_since"] = None
                return ok
            self._check_exits()
            if j["result"] is not None:
                j["state"] = DONE if j["result"].get("rc") == 0 else FAILED
                return False
        if j["spec"].get("start_cmd") and not j["driver"].pids():
            return self._start(name, reason)
        ok = self._transition(j, name, "resume", reason)
        if ok:
            j["waiting_since"] = None
        return ok

    def _start(self, name, reason=""):
        """Run a job from its recipe: start_cmd in workdir, in its own process group, output to
        <state_dir>/job-<name>.log. A small shell wrapper writes the exit code to job-<name>.result.json when the
        command finishes, so GapRunner learns it even if GapRunner itself was down at the time. A park (SIGTERM to
        the group) ends the wrapper too, so a parked job leaves no result and is started again later."""
        j, sp = self.job[name], self.job[name]["spec"]
        res = self._result_path(name)
        if os.path.exists(res):
            os.remove(res)
        wrapper = (f"{sp['start_cmd']}\nrc=$?\n"
                   f"printf '{{\"rc\": %d, \"t\": %s}}\\n' \"$rc\" \"$(date +%s)\" > '{res}.tmp' && mv '{res}.tmp' '{res}'\n"
                   f"exit $rc\n")
        log = open(os.path.join(self.top["state_dir"], f"job-{_slug(name)}.log"), "a")
        try:
            env = {**os.environ, **sp.get("env", {})}
            if sp.get("target_service"):
                svc = self.svc[sp["target_service"]]
                env.update(GAPRUNNER_URL=f"http://{svc['service']['listen']}/_job/{_slug(name)}",
                           GAPRUNNER_SERVICE=sp["target_service"], GAPRUNNER_ENGINE=svc["engine"])
            j["proc"] = subprocess.Popen(["sh", "-c", wrapper], cwd=os.path.expanduser(sp.get("workdir", ".")),
                                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         start_new_session=True, env=env)
        except OSError as e:
            self.audit("start_failed", target=name, error=str(e)[:300])
            return False
        finally:
            log.close()
        st = D.start_time(j["proc"].pid)
        with open(sp["wrapper_pid_file"], "w") as f:
            f.write(f"{j['proc'].pid} {st}\n" if st is not None else f"{j['proc'].pid}\n")
        j["state"], j["waiting_since"], j["result"], j["expect_exit"] = HOT, None, None, False
        self.audit("start", target=name, pid=j["proc"].pid, reason=reason)
        return True

    def _check_exits(self):
        for n, j in self.job.items():
            p = j["proc"]
            if p is not None and p.poll() is not None:
                j["proc"] = None
                self._close_gpu_peak(n)
                if j.pop("expect_exit", False):
                    # a park that stops only the job's own process (a command driver's park_cmd) leaves the wrapper
                    # alive to record the exit code; a stopped job hasn't finished, so that record is dropped
                    if os.path.exists(self._result_path(n)):
                        os.remove(self._result_path(n))
                    self.audit("job_exit", target=n, rc=p.returncode, note="stopped by park; restarts from its recipe")
                    continue
                j["result"] = _read_json(self._result_path(n)) or {"rc": p.returncode, "t": round(self.clock(), 1)}
                with open(self._result_path(n), "w") as f:
                    json.dump(j["result"], f)
                self.audit("job_exit", target=n, rc=j["result"]["rc"])
                from . import health
                rc = j["result"]["rc"]
                health.notify(self, "info" if rc == 0 else "warn", n, f"{n}: finished" if rc == 0 else f"{n}: failed (exit {rc})")

    # ---------- the loop ----------
    def inspect(self):
        """Read every service's and job's state once, the read-only way (`gaprunner check`): it runs the recipes'
        inspection commands (pids_cmd, pgrep, state_cmd) and reads result files, and writes nothing."""
        for e in self.svc.values():
            e["state"] = e["driver"].state()
        for n, e in self.job.items():
            e["state"] = e["driver"].state()
            if e["spec"].get("start_cmd") and e["result"] is None:
                e["result"] = _read_json(self._result_path(n))

    def refresh(self):
        self._check_exits()
        self._claims_and_holds()
        for e in self.svc.values():
            if e["stopped"]:
                e["state"] = STOPPED
            elif e["starting_since"] is not None:
                e["state"] = STARTING         # _windows() makes it HOT once ready; until then decide() must not park it
            else:
                e["state"] = e["driver"].state()
        for n, e in self.job.items():
            if e["proc"] is not None and e["proc"].poll() is None and not e["driver"].pids():
                e["state"] = HOT              # launched by us and still starting up: its processes may not exist yet
                continue
            if e["spec"].get("start_cmd") and not e["driver"].pids():
                if e["proc"] is not None:
                    self._check_exits()        # it exited during this refresh: collect its result, never restart it
                if e["result"] is None and e["proc"] is None:
                    e["result"] = _read_json(self._result_path(n))     # finished while GapRunner was not watching
                rc = (e["result"] or {}).get("rc")
                e["state"] = PARKED if rc is None else DONE if rc == 0 else FAILED
            else:
                e["state"] = e["driver"].state()

    def snapshot(self):
        now = self.clock()
        reading = self.gpu.read()
        total = self.top["gpu_total_gib"] or (reading or {}).get("total_gib") or 0.0
        q = C.queue_overrides(self.top)
        services = [Service(n, e["state"], e["inflight"] + e["job_inflight"], max(e["last_done"], e.get("job_last_done", 0)),
                            e["spec"]["gpu_gib"], e["service"]["idle_park_s"],
                            self.costs.get(e["cost_key"], "resume")[0],
                            self.host_park_gib(e["cost_key"],
                                               float(e["spec"]["parked"]["cpu_gib"] or 0) if e["released"] else
                                               max(0.0, float(e["spec"]["parked"]["cpu_gib"] or 0) -
                                                   float(e["spec"]["running"]["cpu_gib"] or 0))),
                            warming=e.get("warm_since") is not None and
                            now - e["warm_since"] < e["spec"].get("warm_hold_max_s", 300))
                    for n, e in self.svc.items()]
        jobs = []
        for n, e in self.job.items():
            sp = {**e["spec"], **{k: v for k, v in q.get(n, {}).items() if v is not None}}
            waiting = e["driver"].waiting_since() if hasattr(e["driver"], "waiting_since") else e["waiting_since"]
            jobs.append(Job(n, e["state"], sp["gpu_gib"], self.costs.get(n, "park")[0], self.costs.get(n, "resume")[0],
                            sp["order"], sp["hold"], sp["window_only"], sp["enabled"], waiting, e["parked_at"],
                            sp["coexist"], n in self.calibrating or e["frozen"], e.get("health") or None, self._blocked_by(e, n),
                            max(0.0, float(sp["running"]["cpu_gib"] or 0) - float(sp["parked"]["cpu_gib"] or 0)),
                            float(self.costs.get(n, "save")[0] or 0) if sp["loses_work"] else 0.0, sp["loses_work"],
                            frozen=e["frozen"]))
        return Snapshot(now, services, jobs, total, (reading or {}).get("used_gib"), C.window_open(self.top),
                        self.paused(), C.exclusive_window(self.top), self.holds(), self.host_mem())

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return []                      # a wake is in progress; it owns the GPU this instant
        try:
            self.refresh()
            if self.governor is not None:
                self.governor.tick()
            snap = self.snapshot()
            acts = decide(snap, {n: e["gaps"] for n, e in self.svc.items()}, self.policy)
            plan = [(a.kind, a.target) for a in acts]
            if not self.live:
                if plan != self.last_plan and acts:
                    self.audit("would", actions=[a.__dict__ for a in acts])
                self.last_plan = plan
                return acts
            idle = min((self.clock() - e["last_done"] for e in self.svc.values()), default=0)
            if not any(e["inflight"] for e in self.svc.values()):
                for jn, j in self.job.items():
                    if j["paused"] and not j["frozen"] and idle >= j["spec"]["yield_resume_s"]:
                        j["driver"].unpause()
                        j["paused"] = False
                        self.audit("yield_resume", target=jn, reason=f"services idle {idle:.0f}s")
            for a in acts:
                if a.kind == "park_service":
                    s = self.svc[a.target]
                    s["state"] = PARKING            # set first, then re-check: see ensure_hot
                    if s["inflight"]:
                        s["state"] = HOT
                        continue
                    if self._transition(s, a.target, "park", a.reason):
                        s["released"] = False
                elif a.kind == "wake_service":
                    self._transition(self.svc[a.target], a.target, "resume", a.reason)
                elif a.kind == "park_job":
                    self._park_job(a.target, a.reason)
                elif a.kind == "resume_job":
                    self._resume_job(a.target, a.reason)
            self._windows()
            self._recover()
            self._schedules()
        finally:
            self.lock.release()
        # hooks that run commands (release_cmd, progress_cmd, health_cmd) run outside the transition lock, so a
        # slow or hung one can never delay a service's wake
        from . import health
        self._release_if_short(snap)
        self._track_progress()
        self._track_speed()
        health.check(self)
        return acts

    def _track_speed(self):
        """Tokens per second for running jobs and services that expose Prometheus counters (speed.py)."""
        from . import speed
        now = self.clock()
        for e in self.svc.values():
            speed.update(e, speed.url_of(e["spec"]), now, e["state"] == HOT)
        for j in self.job.values():
            speed.update(j, speed.url_of({"metrics_url": j["spec"].get("metrics_url")}), now,
                         j["state"] == HOT and not j["frozen"])


    def holders(self, reading):
        """({name: GiB on the GPU}, {name: measured?}, stowaway procs). Matched by pid when the config gives pids
        (`pids`, `pid_file` or `pgrep`); a hot entry without pids is counted at its configured size, as an estimate.
        Stowaways are GPU processes no entry claims by pid (give every entry pids to keep this list honest)."""
        procs = (reading or {}).get("procs", [])
        claimed, gib, measured = set(), {}, {}
        for sn, sv in self.svc.items():           # a service's other engines (asleep, or starting) are its own
            for en, e in sv["engines"].items():
                if en == sv["engine"]:
                    continue
                mine = [p for p in procs if p["pid"] in set(e["driver"].pids())]
                claimed.update(p["pid"] for p in mine)
                if mine:
                    gib[f"{sn}: {en}"], measured[f"{sn}: {en}"] = round(sum(p["gib"] for p in mine), 2), True
        for c in self.claims:                     # hand-run work named by a claim is counted, not "untracked"
            mine = [p for p in procs if p["pid"] in set(c["pids"])]
            claimed.update(p["pid"] for p in mine)
            if mine:
                gib[f"claim: {c['spec']['name']}"] = round(sum(p["gib"] for p in mine), 2)
                measured[f"claim: {c['spec']['name']}"] = True
        guess = []
        for n, e in {**self.svc, **self.job}.items():
            pids = set(e["driver"].pids())
            mine = [p for p in procs if p["pid"] in pids]
            claimed.update(p["pid"] for p in mine)
            if mine:
                gib[n], measured[n] = round(sum(p["gib"] for p in mine), 2), True
            elif e["state"] == HOT:
                guess.append(n)
            else:
                gib[n], measured[n] = 0.0, True
        stow = [p for p in procs if p["pid"] not in claimed]
        # an entry seen only as "hot" (no pids found) is counted at its recipe size, but only out of the GPU memory
        # nothing else accounts for, so a guess never double-counts unknown processes or exceeds the GPU
        left = max(0.0, (reading or {}).get("used_gib", 0.0) - sum(gib.values()) - sum(p["gib"] for p in stow)) if reading else None
        for n in guess:
            g = float({**self.svc, **self.job}[n]["spec"]["gpu_gib"])
            gib[n], measured[n] = (round(min(g, left), 2) if left is not None else g), False
            if left is not None:
                left -= gib[n]
        return gib, measured, stow

    def sample(self):
        reading = self.gpu.read()
        now = self.clock()
        gib, measured, stow = self.holders(reading)
        for n, e in self.job.items():           # each run's GPU peak, shown beside the recipe's figure
            if e["state"] == HOT and measured.get(n) and gib.get(n):
                e["gpu_peak_now"] = max(e.get("gpu_peak_now", 0.0), gib[n])
        busy = {n: bool(e["inflight"]) for n, e in self.svc.items()}
        row = {"t": round(now, 1), "used": (reading or {}).get("used_gib"), "total": (reading or {}).get("total_gib"),
               "h": gib, "stow": round(sum(p["gib"] for p in stow), 2),
               "st": {n: e["state"] for n, e in {**self.svc, **self.job}.items()}, "busy": busy,
               "util": (reading or {}).get("util"), "power": (reading or {}).get("power_w"),
               "tps": {n: e["tps"]["out"] for n, e in {**self.svc, **self.job}.items() if e.get("tps")}}
        self.live_rows.append(row)
        if int(now // 60) != self.last_minute:
            self.last_minute = int(now // 60)
            with open(self.history_path, "a") as f:
                f.write(json.dumps(row) + "\n")
        return row

    def history(self, seconds):
        """GPU memory rows: minute rows from disk, plus the 5 s rows kept in memory for the last hour (so a restart
        doesn't blank the chart: before the first in-memory row, the minute rows fill in)."""
        since = self.clock() - seconds
        recent = [r for r in self.live_rows if r["t"] >= since]
        older_than = recent[0]["t"] if recent else self.clock()
        return [r for r in _tail_jsonl(self.history_path, since) if r["t"] < older_than] + recent

    def activity_since(self, seconds):
        since = self.clock() - seconds
        if seconds <= 86400:
            return [r for r in self.activity if r["t"] >= since]
        return _tail_jsonl(self.activity_path, since)

    def start_calibration(self, name, warm_s=60.0, cycles=2):
        """Run gaprunner.calibrate for one job in a background thread (one calibration at a time)."""
        from . import calibrate
        if name not in self.job:
            raise ValueError("no such job (calibration needs a [[job]] recipe)")
        if self.calibrating:
            raise ValueError(f"already calibrating {next(iter(self.calibrating))}")
        threading.Thread(target=calibrate.run, args=(self, name, float(warm_s), int(cycles)), daemon=True,
                         name=f"calibrate-{name}").start()
        return f"Measuring {name}: it runs, steps aside and comes back {cycles}x"

    def recent_events(self, n=200):
        """The last n audit rows, newest first (reads only the end of the file)."""
        try:
            with open(self.audit_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 400 * n))
                lines = f.read().decode(errors="replace").splitlines()[1:]
        except FileNotFoundError:
            return []
        rows = []
        for line in lines[-n:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows[::-1]

    def runs(self):
        """Past runs per job from the audit log (30 days): running time of each finished run (start to exit,
        minus time stepped aside) and the run in progress, so the UI can estimate how long queued work takes.
        {name: {"finished": [{"run_s", "rc", "t", "progress"}], "current_s": seconds running in the open run}}"""
        out, open_at, acc = {}, {}, {}
        try:
            with open(self.audit_path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    n, ev, t = e.get("target"), e.get("event"), e.get("t", 0)
                    if not n or n not in self.job:
                        continue
                    if ev in ("start", "resume", "thaw"):
                        open_at.setdefault(n, t)
                    elif ev in ("park", "freeze", "job_exit", "job_killed") and n in open_at:
                        acc[n] = acc.get(n, 0.0) + max(0.0, t - open_at.pop(n))
                    if ev == "job_exit":
                        r = out.setdefault(n, {"finished": [], "current_s": 0.0})
                        r["finished"].append({"run_s": round(acc.pop(n, 0.0)), "rc": e.get("rc"), "t": t})
        except FileNotFoundError:
            pass
        now = self.clock()
        for n in self.job:
            r = out.setdefault(n, {"finished": [], "current_s": 0.0})
            r["current_s"] = round(acc.get(n, 0.0) + (now - open_at[n] if n in open_at else 0.0))
            r["finished"] = r["finished"][-5:]
        return out

    def status(self):
        reading = self.gpu.read()
        gib, measured, stow = self.holders(reading)
        now = self.clock()
        snap = self.snapshot()
        why = explain(snap, {n: e["gaps"] for n, e in self.svc.items()}, self.policy)
        for n, e in self.job.items():
            if e["frozen"]:
                why[n] = ("frozen", "paused by you where it was; it keeps its memory. Resume continues it")
        q = C.queue_overrides(self.top)
        from . import __version__
        out = {"t": round(now, 3), "version": __version__, "mode": self.top["mode"], "paused": self.paused(), "hold": self.holds(),
               "draining": os.path.exists(self.drain_path), "gpu_hold": _read_json(self.gpu_hold_path),
               "cpu_free_gib": self.host_mem(), "cpu_total_gib": _mem_total_gib(), "servers": self.model_servers, "claims": [{"name": c["spec"]["name"], "action": c["spec"]["action"],
                                                             "pids": c["pids"]} for c in self.claims],
               "window_open": C.window_open(self.top), "window_until": C.window_until(self.top),
               "window_exclusive": C.exclusive_window(self.top),
               "gpu": reading, "holders": gib, "measured": measured, "stowaways": [_proc_info(p) for p in stow],
               "costs": self.costs.table(),
               "policy": self.policy.__dict__, "services": {}, "jobs": {}, "recent": self.recent[-20:]}
        for n, e in self.svc.items():
            idle = 0.0 if e["inflight"] else now - e["last_done"]
            m = e["gaps"]
            out["services"][n] = {"state": e["state"], "inflight": e["inflight"], "idle_s": round(idle, 1),
                                  "last_request_t": round(e["last_done"], 1),
                                  "running": e["spec"]["running"], "parked": e["spec"]["parked"],
                                  "unavailable": self.unavailable(n), "can_stop": bool(e["spec"].get("stop_cmd")),
                                  "health": e.get("health") or [], "released": e["released"],
                                  "engine": e["engine"], "switching": e["switching"], "job_inflight": e["job_inflight"], "tps": e.get("tps"),
                                  "warming_s": None if e.get("warm_since") is None else round(now - e["warm_since"], 1),
                                  "tps_last": e.get("tps_last"), "stats": e.get("stats"),
                                  "schedule": e["service"].get("schedule", []),
                                  "engines": {en: {"running": x["spec"]["running"], "parked": x["spec"]["parked"],
                                                   "upstream": x["spec"]["upstream"], "switch_out": x["spec"].get("switch_out"),
                                                   "current": en == e["engine"], "notes": x["spec"].get("notes", "")}
                                              for en, x in e["engines"].items()},
                                  "cpu_gib_now": _rss_gib(e["driver"].pids()),
                                  "host_park_gib": self.costs.get(e["cost_key"], "park_host")[0]
                                  if self.costs.get(e["cost_key"], "park_host")[1] else None,
                                  "gpu_gib": e["spec"]["gpu_gib"], "idle_park_s": e["service"]["idle_park_s"],
                                  "driver": e["spec"]["driver"], "gap_samples": len(m),
                                  "p_idle_30s_more": m.p_continue(idle, 30), "median_more_idle_s": m.median_remaining(idle),
                                  "survival": [[x, m.p_continue(x, 30)] for x in (0, 5, 10, 20, 30, 60, 120, 300, 600)]}
        for j in snap.jobs:
            e = self.job[j.name]
            out["jobs"][j.name] = {"state": e["state"], "order": j.order, "hold": j.hold, "window_only": j.window_only,
                                   "coexist": j.coexist, "paused": e["paused"], "running": e["spec"]["running"],
                                   "parked": e["spec"]["parked"], "cpu_gib_now": _rss_gib(e["driver"].pids()),
                                   "host_park_gib": self.costs.get(j.name, "park_host")[0]
                                   if self.costs.get(j.name, "park_host")[1] else None,
                                   "gpu_peak_gib": self.gpu_peak_gib(j.name), "gpu_peak_now": e.get("gpu_peak_now"),
                                   "workdir": e["spec"].get("workdir"), "start_cmd": e["spec"].get("start_cmd"),
                                   "result": e["result"], "ssd_paths": e["spec"].get("ssd_paths", []),
                                   "goal": e["spec"].get("goal"), "progress": e["progress"], "health": e.get("health") or [],
                                   "frozen": e["frozen"], "part": e.get("part"), "part_text": e.get("part_text", ""),
                                   "frozen_since": _mtime(self._frozen_path(j.name)) if e["frozen"] else None,
                                   "gave_way": e.get("gave_way", 0), "notes": e["spec"].get("notes", ""),
                                   "log": e["spec"].get("log_file") or (os.path.join(self.top["state_dir"], f"job-{_slug(j.name)}.log")
                                                                         if e["spec"].get("start_cmd") else None),
                                   "after": self.after_of(j.name), "tps": e.get("tps"), "tps_last": e.get("tps_last"), "stats": e.get("stats"), "max_hours": e["spec"].get("max_hours"),
                                   "target_service": e["spec"].get("target_service"),
                                   "requires_engine": e["spec"].get("requires_engine"),
                                   "stops_at_saved_point": bool(e["spec"].get("saved_cmd")), "saved_base": e.get("saved_base"),
                                   "loses_work": e["spec"]["loses_work"], "save_every_s": self.costs.get(j.name, "save")[0],
                                   "save_every_measured": self.costs.get(j.name, "save")[1], "unsaved_s": self.unsaved_s(j.name),
                                   "progress_text": e["progress_text"], "run_hours": round(e["run_s"] / 3600, 2),
                                   "gpu_hours_left": _hours_left(e),
                                   "calibrating": self.calibrating.get(j.name, {}).get("phase"),
                                   "calibration": _calib_summary(self, j.name),
                                   "gpu_gib": j.gpu_gib, "driver": e["spec"]["driver"], "kind": e["spec"].get("kind", ""),
                                   "queued": j.name in q, "tags": self.tags_of(j.name), "summary": e.get("summary"),
                                   "color": e["spec"].get("color"),
                                   "started_t": round(e["active_since"], 1) if e["state"] == HOT and e.get("active_since") else None,
                                   "waiting_s": None if e["waiting_since"] is None else round(now - e["waiting_since"], 1),
                                   "word": why[j.name][0], "why": why[j.name][1]}
        if self.governor is not None:
            for j in self.governor.by_owner.values():   # whether its app would be admitted now, and if not, why
                not_now = self.governor._not_now(j)
                out["jobs"][j.spec["name"]].update(admits_now=not_now is None, not_now=not_now)
        order = sorted(out["jobs"].items(), key=lambda kv: kv[1]["order"])
        nxt = next(((n, j) for n, j in order if j["word"] in ("next", "waiting")), None)
        out["next"] = {"name": nxt[0], "word": nxt[1]["word"], "why": nxt[1]["why"]} if nxt else None
        return out

    def save(self):
        for n, e in self.svc.items():
            e["gaps"].save(self._gap_path(n))
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.status(), f, indent=1, default=str)
        os.replace(tmp, self.status_path)

    def _discover_forever(self):
        """Name model servers GapRunner doesn't manage (servers.py), on its own thread: probing ports can take a few
        seconds and must never hold up scheduling."""
        from . import servers
        while True:
            try:
                skip = [m for s in self.svc.values() for e in s["engines"].values() for m in (e["spec"].get("model_name"),) if m]
                self.model_servers = self.finder.refresh(servers.known_ports(self), skip_models=skip + list(self.svc))
            except Exception:  # noqa: BLE001 -- discovery is a nicety; it never breaks anything
                self.model_servers = []
            time.sleep(servers.SCAN_S)

    def run_forever(self):
        threading.Thread(target=self._discover_forever, daemon=True, name="discover").start()
        if self.governor is not None:
            self.governor.start()
        self.settle_until = self.clock() + 15
        self.refresh()
        self.audit("start", mode=self.top["mode"], services=list(self.svc), jobs=list(self.job),
                   states={n: e["state"] for n, e in {**self.svc, **self.job}.items()})
        last_save = last_sample = 0.0
        while True:
            try:
                self.tick()
                if self.clock() - last_sample >= 5:
                    self.sample()
                    last_sample = self.clock()
                if self.clock() - last_save >= 15:
                    self.save()
                    last_save = self.clock()
            except Exception as e:  # noqa: BLE001 -- the loop must survive; failures go to the audit log
                self.audit("tick_error", error=repr(e)[:300])
            time.sleep(self.top["tick_s"])


def _tail_jsonl(path, since):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= since:
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def _trim_jsonl(path, since):
    rows = _tail_jsonl(path, since)
    if not os.path.exists(path):
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    os.replace(tmp, path)


def _slug(name):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower()).strip("-")[:60]


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _rss_gib(pids):
    """Resident CPU memory of these processes (VmRSS), GiB; None when nothing is readable (e.g. another user's)."""
    total, seen = 0, False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
                        seen = True
        except OSError:
            pass
    return round(total / 1024 / 1024, 2) if seen else None


def _calib_summary(runner, name):
    from . import calibrate
    m = calibrate.load(runner, name)
    if not m:
        return None
    return {k: m.get(k) for k in ("t", "status", "notes", "running", "running_peak", "parked", "park_s", "resume_s", "toml",
                                  "gpu_hours_total", "gpu_hours_left")}


def _hours_left(e):
    from . import progress
    return progress.hours_left(e["progress"], progress.rate(list(e["prog"])))


def _mem_available_gib():
    """Host memory available (MemAvailable), GiB; None where /proc/meminfo doesn't exist."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return None


def _mem_total_gib():
    """Host memory total (MemTotal), GiB; None where /proc/meminfo doesn't exist."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return None


def _mtime(path):
    try:
        return round(os.path.getmtime(path), 1)
    except OSError:
        return None


def _proc_info(p):
    """A GPU process GapRunner doesn't know: who owns it, when it started, its command line (where /proc allows)."""
    info = dict(p)
    try:
        import pwd
        st = os.stat(f"/proc/{p['pid']}")
        info["user"] = pwd.getpwuid(st.st_uid).pw_name
        info["started"] = round(st.st_mtime, 1)
        with open(f"/proc/{p['pid']}/cmdline", "rb") as f:
            info["cmd"] = f.read().replace(b"\0", b" ").decode(errors="replace").strip()[:300]
    except (OSError, KeyError, ImportError):
        pass
    return info
