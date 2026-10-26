"""The decision core. Pure functions over a snapshot: no I/O, no clocks, so the live daemon and `gaprunner replay`
run exactly the same rules.

Each job runs in the cheapest placement that fits:
  beside  the job fits in GPU memory next to the hot services (coexist = "share" or "yield"): no service parks,
          no request waits. "yield" jobs also pause their compute while a service has requests in flight.
  gap     the job needs memory a service holds: the service parks and the job runs until the next request.
  window  the job is too slow to hand the GPU back within max_added_wait_s; it runs only in windows.

Rules, in order:
  1. Kill switch (paused): park every job, wake every service. Nothing else happens.
  2. Services always win. A request for a parked service is handled by the gate (plan_wake), not here.
  2b. Jobs that may coexist start beside the hot services whenever they fit (reserve_gib kept free).
  3. Park an idle service once it has had no request for `idle_park_s` -- but only if some job is waiting to use the
     GPU and the current gap already looks worth it for that job (park_only_if_work), since parking for nothing
     just adds wake latency.
  4. Admit (resume) waiting jobs into the gap while every service is parked, when all of these hold:
       - not held; window_only jobs only inside a window;
       - its reclaim cost (job park + slowest service wake) is within max_added_wait_s, otherwise windows only;
       - the gap is likely to last: P(idle >= worth_factor * (park + resume) more | idle so far) >= p_min,
         or, with too little history, idle so far >= cold_start_after_s;
       - it fits in GPU memory (committed and driver-measured), keeping reserve_gib free;
       - it was not parked within juggle_s (anti-thrash).
     Order: jobs waiting longer than starvation_s first, then the human queue order.
"""
from dataclasses import dataclass, field

HOT, PARKED, PARKING, RESUMING, UNKNOWN = "hot", "parked", "parking", "resuming", "unknown"
DONE, FAILED = "done", "failed"
STOPPED, STARTING = "stopped", "starting"   # a service stopped for an exclusive window, and coming back           # a job started from its recipe that has exited (0 / non-zero)


@dataclass
class Service:
    name: str
    state: str = HOT
    inflight: int = 0
    last_done: float = 0.0
    gpu_gib: float = 0.0
    idle_park_s: float = 30.0
    wake_s: float = 1.0
    park_cpu_gib: float = 0.0      # extra CPU memory it takes when parked (its weights moving to host RAM)


@dataclass
class Job:
    name: str
    state: str = PARKED
    gpu_gib: float = 0.0
    park_s: float = 5.0
    resume_s: float = 5.0
    order: int = 0
    hold: bool = False
    window_only: bool = False
    enabled: bool = True
    waiting_since: float = None
    parked_at: float = None
    coexist: str = "no"            # no | share | yield
    pinned: bool = False           # being calibrated: the scheduler neither starts nor parks it
    issues: list = None            # health problems found while it runs (report-only)
    blocked_by: str = None         # a dependency that hasn't finished (or failed), in words
    run_cpu_gib: float = 0.0       # extra CPU memory it takes when it starts or resumes
    save_s: float = 0.0            # loses unsaved work when it steps aside: running time to its next saved point
    loses_work: bool = False       # stepping aside throws away work since its last saved point (restart jobs)


@dataclass
class Policy:
    p_min: float = 0.75
    worth_factor: float = 3.0
    max_added_wait_s: float = 5.0
    wake_deadline_s: float = 0.0    # when a request wakes a service, jobs get this long to step aside before a stuck one
                                    # is stopped hard (kill_stuck_jobs); 0 = 3 x max_added_wait_s. A job whose measured
                                    # step-aside time is longer still gets twice that, so a slow-but-normal job isn't killed
    juggle_s: float = 15.0
    starvation_s: float = 3600.0
    reserve_gib: float = 4.0
    cold_start_after_s: float = 120.0
    park_only_if_work: bool = True
    cpu_floor_gib: float = 16.0     # never start or park something if host memory would drop below this
    max_active_jobs: int = 0        # at most this many jobs on the GPU at once (0 = no limit); the rest wait in
                                    # queue order, like qBittorrent's "Max active torrents"
    lossy_p_min: float = 0.5        # a job that loses unsaved work starts in a quiet spell only when it's at least
                                    # this likely to last until the job's next saved point


@dataclass
class Snapshot:
    now: float
    services: list
    jobs: list
    gpu_total_gib: float
    gpu_used_gib: float = None      # driver reading; None when unavailable
    window_open: bool = False
    paused: bool = False
    exclusive: bool = False         # an exclusive window is open: services are being stopped
    hold: str = None                # why nothing new may start right now (drain, claim, GPU hold, settling), or None
    cpu_free_gib: float = None      # host memory available (MemAvailable); None when unknown


@dataclass(frozen=True)
class Action:
    kind: str       # park_service | wake_service | park_job | resume_job
    target: str
    reason: str = field(default="", compare=False)


def committed_gib(snap):
    s = sum(x.gpu_gib for x in snap.services if x.state in (HOT, RESUMING, PARKING, UNKNOWN, STARTING))
    j = sum(x.gpu_gib for x in snap.jobs if x.state in (HOT, RESUMING, PARKING, UNKNOWN))
    return s + j


def active_jobs(snap):
    """Jobs on the GPU or coming onto it: what policy.max_active_jobs counts."""
    return [j for j in snap.jobs if j.state in (HOT, RESUMING)]


def idle_elapsed(snap):
    """Seconds since the most recent service finished its last request (the current gap)."""
    if any(s.inflight for s in snap.services):
        return 0.0
    return max(0.0, snap.now - max((s.last_done for s in snap.services), default=snap.now))


def reclaim_s(snap, job):
    return job.park_s + max((s.wake_s for s in snap.services), default=0.0)


def eligible(snap, job, policy):
    """(ok, reason) -- may this job run in gaps at all (ignoring the current gap and memory)?"""
    if not job.enabled:
        return False, "disabled"
    if job.hold:
        return False, "held"
    if job.blocked_by:
        return False, job.blocked_by
    if job.window_only or reclaim_s(snap, job) > policy.max_added_wait_s:
        if not snap.window_open:
            return False, "windows only" if job.window_only else \
                f"reclaim {reclaim_s(snap, job):.1f}s > {policy.max_added_wait_s:.0f}s: windows only"
        return True, "window open"
    return True, "gap job"


def gap_worth_it(snap, job, gaps, policy):
    """(ok, reason) -- is the current gap likely to last long enough to pay for this job's swap?"""
    if snap.window_open:
        return True, f"no {_svc_names(snap)} is on" if snap.exclusive else f"slow-wake {_svc_names(snap)} is on"
    elapsed = idle_elapsed(snap)
    need = policy.worth_factor * (job.park_s + job.resume_s)
    ps = [m.p_continue(elapsed, need) for m in gaps.values()] if gaps else []
    if not ps or any(p is None for p in ps):
        ok = elapsed >= policy.cold_start_after_s
        return ok, (f"still learning how long {_svc_names(snap)} stays quiet; meanwhile it waits for "
                    f"{_dur(policy.cold_start_after_s)} of quiet ({_dur(elapsed)} so far)")
    if job.save_s:
        need_save = job.resume_s + job.save_s
        p_save = min(m.p_continue(elapsed, need_save) for m in gaps.values())
        bar = max(policy.p_min, policy.lossy_p_min)
        if p_save < bar:
            return False, (f"it loses unsaved work when it steps aside, so it needs about {_dur(need_save)} of quiet to "
                           f"reach its next saved point; after {_dur(elapsed)} of quiet that happens {p_save:.0%} of "
                           f"the time (needs {bar:.0%})")
    p = min(ps)
    return p >= policy.p_min, (f"it needs about {_dur(need)} of quiet to be worth stepping in; after {_dur(elapsed)} of "
                               f"quiet that happens {p:.0%} of the time (needs {policy.p_min:.0%})")


def _dur(seconds):
    seconds = float(seconds)
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def _svc_names(snap):
    names = [s.name for s in snap.services]
    return " and ".join(names) if names else "the service"


def decide(snap, gaps, policy):
    """Actions to take now. `gaps` maps service name -> GapModel."""
    if snap.paused:
        return [Action("park_job", j.name, "paused") for j in snap.jobs if j.state in (HOT, RESUMING)] + \
               [Action("wake_service", s.name, "paused") for s in snap.services if s.state in (PARKED, PARKING)]

    acts = []
    # a job that may only run in a window is parked when the window closes (it would make requests wait too long)
    for j in snap.jobs:
        if j.state == HOT and not j.pinned and j.blocked_by:
            # a running job can only become blocked when its required engine stops serving: it steps aside
            acts.append(Action("park_job", j.name, j.blocked_by))
        elif j.state == HOT and not j.pinned and not eligible(snap, j, policy)[0] and \
                eligible(snap, j, policy)[1] not in ("held", "disabled"):
            acts.append(Action("park_job", j.name, "window closed"))
    if snap.hold:
        return acts                      # drain, claim, GPU hold or settling: nothing new starts, nothing parks
    limit = policy.max_active_jobs
    if limit:
        # more jobs running than the limit allows (it was just lowered): the last in the queue step aside
        on = sorted((j for j in active_jobs(snap) if not j.pinned and j.state == HOT), key=lambda j: j.order)
        pinned = sum(1 for j in active_jobs(snap) if j.pinned or j.state != HOT)
        for j in on[max(0, limit - pinned):]:
            acts.append(Action("park_job", j.name, f"over max active jobs ({limit})"))
    slots = [limit - len(active_jobs(snap)) if limit else None]     # None: no limit
    waiting = [j for j in snap.jobs if j.state == PARKED and not j.pinned and eligible(snap, j, policy)[0]
               and not (j.parked_at is not None and snap.now - j.parked_at < policy.juggle_s)]
    busy = any(s.inflight for s in snap.services)

    def rank(j):
        starving = j.waiting_since is not None and snap.now - j.waiting_since >= policy.starvation_s
        return (0 if starving else 1, j.order)

    # a job that just stepped aside waits juggle_s before it may come back; its slot is kept for it meanwhile, so a job
    # lower in the queue doesn't take the slot and hold it until the next step-aside
    returning = [j for j in snap.jobs if j.state == PARKED and not j.pinned and eligible(snap, j, policy)[0]
                 and j.parked_at is not None and snap.now - j.parked_at < policy.juggle_s]

    def has_slot(j=None):
        if slots[0] is None:
            return True
        return slots[0] - sum(1 for r in returning if j is None or rank(r) < rank(j)) > 0

    # beside: jobs that may coexist start whenever they fit next to whatever is on the GPU now
    free = snap.gpu_total_gib - max(committed_gib(snap), snap.gpu_used_gib or 0.0) - policy.reserve_gib
    cpu = [snap.cpu_free_gib]
    cpu_ok = lambda need: cpu[0] is None or cpu[0] - need >= policy.cpu_floor_gib
    # a job started beside a sleeping service must step aside when it wakes unless it also fits beside the awake
    # service; those step-asides are serial, so they share the reclaim budget with the gap jobs below
    room_awake = snap.gpu_total_gib - policy.reserve_gib - sum(sv.gpu_gib for sv in snap.services) - \
        sum(j.gpu_gib for j in snap.jobs if j.coexist != "no" and j.state in (HOT, RESUMING))
    side_reclaim = max((sv.wake_s for sv in snap.services), default=0.0) + \
        sum(j.park_s for j in snap.jobs if j.coexist == "no" and j.state in (HOT, RESUMING))
    if limit and not has_slot():
        # queue order decides who holds the slots: a job higher in the queue that could start now pushes the active
        # job lowest in the queue aside, if that job keeps its place when it steps aside (one that would lose work
        # keeps its slot until it steps aside or finishes). Queue position only: the starvation guard reorders
        # waiting jobs, it never pushes a running one aside.
        want = sorted((j for j in waiting if not (j.coexist == "yield" and busy)), key=lambda j: j.order)
        movable = sorted((j for j in snap.jobs if j.state == HOT and not j.pinned and not j.loses_work),
                         key=lambda j: -j.order)
        if want and movable and want[0].order < movable[0].order:
            w, a = want[0], movable[0]
            asleep = all(s.state in (PARKED, STOPPED) for s in snap.services)
            could = (w.gpu_gib <= free + a.gpu_gib and cpu_ok(w.run_cpu_gib) and
                     (gap_worth_it(snap, w, gaps, policy)[0] if asleep else w.coexist != "no"))
            if could and not any(x.kind == "park_job" and x.target == a.name for x in acts):
                acts.append(Action("park_job", a.name, f"makes room for {w.name}, higher in the queue "
                                                        f"(max active jobs {limit})"))
                return acts
    for j in sorted(waiting, key=rank):
        if not has_slot(j):
            break
        if j.coexist == "yield" and busy:
            continue                                  # a yield job starts only while the services are idle
        if j.coexist != "no" and j.gpu_gib <= free and cpu_ok(j.run_cpu_gib):
            if j.gpu_gib <= room_awake:
                room_awake -= j.gpu_gib
            elif not snap.window_open and side_reclaim + j.park_s > policy.max_added_wait_s:
                continue
            elif j.save_s and not gap_worth_it(snap, j, gaps, policy)[0]:
                continue                              # it would step aside at the next wake and lose its work
            else:
                side_reclaim += j.park_s
            free -= j.gpu_gib
            if cpu[0] is not None:
                cpu[0] -= j.run_cpu_gib
            if slots[0] is not None:
                slots[0] -= 1
            acts.append(Action("resume_job", j.name, f"fits beside {_svc_names(snap)} ({j.gpu_gib:.0f} GiB)"))
    started = {a.target for a in acts}
    waiting = [j for j in waiting if j.name not in started]
    worth_now = [j for j in waiting if gap_worth_it(snap, j, gaps, policy)[0]] if has_slot() else []

    for s in snap.services:
        if s.state == HOT and s.inflight == 0 and snap.now - s.last_done >= s.idle_park_s and not snap.exclusive:
            if (worth_now or not policy.park_only_if_work) and cpu_ok(s.park_cpu_gib):
                acts.append(Action("park_service", s.name, f"idle {snap.now - s.last_done:.0f}s"))

    if not all(s.state in (PARKED, STOPPED) for s in snap.services):
        return acts                      # admit jobs only once the services' memory is actually released

    # parks are serial: the next request waits for every job that has to step aside, plus the service wake
    reclaim = side_reclaim
    for j in sorted(waiting, key=rank):
        if not has_slot(j):
            break
        ok, why = gap_worth_it(snap, j, gaps, policy)
        if not ok:
            continue
        if j.gpu_gib > free or not cpu_ok(j.run_cpu_gib):
            continue
        if not snap.window_open and reclaim + j.park_s > policy.max_added_wait_s:
            continue
        if cpu[0] is not None:
            cpu[0] -= j.run_cpu_gib
        free -= j.gpu_gib
        reclaim += j.park_s
        if slots[0] is not None:
            slots[0] -= 1
        acts.append(Action("resume_job", j.name, why))
    return acts


def plan_wake(snap, service=None, reserve_gib=0.0):
    """Jobs to park before waking `service`: every gap job, plus coexisting jobs (lowest priority first) until the
    service fits beside what is left. Without a service name, every job that may hold GPU memory."""
    on_gpu = [j for j in snap.jobs if j.state in (HOT, RESUMING, UNKNOWN)]
    if service is None:
        return [j.name for j in on_gpu]
    need = next(s.gpu_gib for s in snap.services if s.name == service)
    others = sum(s.gpu_gib for s in snap.services if s.name != service and s.state in (HOT, RESUMING, UNKNOWN))
    park = [j for j in on_gpu if j.coexist == "no" or j.state == UNKNOWN]
    keep = sorted((j for j in on_gpu if j not in park), key=lambda j: j.order)
    while keep and others + need + sum(j.gpu_gib for j in keep) > snap.gpu_total_gib - reserve_gib:
        park.append(keep.pop())                       # last in the queue goes first
    return [j.name for j in park]


def explain(snap, gaps, policy):
    """Why each job is (or isn't) on the GPU right now, in words a person can act on. {name: (word, sentence)}.
    word: running | next | held | window | waiting | parking | resuming | unknown | disabled."""
    out = {}
    services_parked = all(s.state in (PARKED, STOPPED) for s in snap.services)
    used = max(committed_gib(snap), snap.gpu_used_gib or 0.0)
    free = snap.gpu_total_gib - used - policy.reserve_gib
    busy = [s.name for s in snap.services if s.state != PARKED]
    for j in sorted(snap.jobs, key=lambda x: x.order):
        if j.state == HOT and getattr(j, "issues", None):
            out[j.name] = ("stalled", "running, but: " + "; ".join(j.issues))
            continue
        if j.state == HOT and snap.exclusive:
            out[j.name] = ("running", f"running while {_svc_names(snap)} is off (no {_svc_names(snap)})")
            continue
        if j.state == HOT and j.window_only and snap.window_open:
            out[j.name] = ("running", f"running during slow-wake {_svc_names(snap)}")
            continue
        if j.state == HOT:
            sv = _svc_names(snap)
            out[j.name] = ("running", f"running alongside {sv}" + (f"; gives way while {sv} answers" if j.coexist == "yield" else "")
                           if j.coexist != "no" else f"running while {sv} sleeps; steps aside when {sv} gets a request")
            continue
        if j.state in (DONE, FAILED):
            out[j.name] = (j.state, "finished" if j.state == DONE else "failed: it exited with an error and isn't retried. "
                                                                         "See its log (Details); Start runs it again")
            continue
        if j.state in (PARKING, RESUMING, UNKNOWN):
            out[j.name] = (j.state, {"parking": "stepping aside", "resuming": "coming back onto the GPU",
                                     "unknown": "state unknown: its driver did not answer"}[j.state])
            continue
        ok, why = eligible(snap, j, policy)
        if not ok and j.blocked_by:
            out[j.name] = ("blocked", j.blocked_by)
            continue
        if not ok:
            word = "disabled" if why == "disabled" else "held" if why == "held" else "window"
            text = {"disabled": "turned off in the config", "held": "wound down: press Start to let it run"}.get(why, why)
            if word == "window":
                sv = _svc_names(snap)
                text = f"runs only during slow-wake {sv}" if j.window_only else \
                    f"too slow to step aside quickly ({sv} would wait {reclaim_s(snap, j):.1f} s); runs during slow-wake {sv}"
            out[j.name] = (word, text)
            continue
        if j.parked_at is not None and snap.now - j.parked_at < policy.juggle_s:
            out[j.name] = ("waiting", f"just stepped aside; waits {policy.juggle_s - (snap.now - j.parked_at):.0f} s before coming back")
            continue
        worth, gwhy = gap_worth_it(snap, j, gaps, policy)
        if snap.hold:
            out[j.name] = ("waiting", snap.hold)
        elif policy.max_active_jobs and len(active_jobs(snap)) < policy.max_active_jobs and (keep := [
                r for r in snap.jobs if r.state == PARKED and r.order < j.order and r.parked_at is not None
                and snap.now - r.parked_at < policy.juggle_s and eligible(snap, r, policy)[0]]) and \
                len(active_jobs(snap)) + len(keep) >= policy.max_active_jobs:
            out[j.name] = ("waiting", f"max active jobs: the free slot is kept for {keep[0].name}, which just stepped "
                                      f"aside and comes back in {policy.juggle_s - (snap.now - keep[0].parked_at):.0f} s")
        elif policy.max_active_jobs and len(active_jobs(snap)) >= policy.max_active_jobs:
            out[j.name] = ("waiting", f"max active jobs reached ({len(active_jobs(snap))} of {policy.max_active_jobs} "
                                      f"running): starts when one finishes or steps aside, in queue order")
        elif snap.cpu_free_gib is not None and snap.cpu_free_gib - j.run_cpu_gib < policy.cpu_floor_gib:
            out[j.name] = ("waiting", f"host memory: needs {j.run_cpu_gib:.0f} GiB, {snap.cpu_free_gib:.0f} GiB available "
                                      f"(floor {policy.cpu_floor_gib:.0f})")
        elif j.coexist != "no" and j.gpu_gib <= free:
            out[j.name] = ("next", f"starting: it fits beside {_svc_names(snap)} ({j.gpu_gib:.0f} of {max(free, 0):.0f} GiB free)")
        elif j.gpu_gib > free and services_parked:
            out[j.name] = ("waiting", f"needs {j.gpu_gib:.0f} GiB, {max(free, 0):.0f} GiB free")
        elif not worth:
            out[j.name] = ("waiting", f"waits for {_svc_names(snap)} to be quiet: {gwhy}")
        elif not services_parked:
            out[j.name] = ("next", f"starts when {', '.join(busy)} goes to sleep")
        else:
            out[j.name] = ("next", f"starting: {gwhy}")
    return out
