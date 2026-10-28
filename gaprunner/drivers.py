"""Drivers: how to park (free the GPU) and resume one service or job. Every driver has the same small interface:

    state() -> "hot" | "parked" | "parking" | "resuming" | "unknown"
    park()          raise on failure; return once the GPU memory is released
    resume()        raise on failure; return once the target is back and ready
    pids() -> [int] processes that belong to it (for the ledger, stowaway report and kill)

Kinds:
  vllm             POST /sleep?level=N, POST /wake_up, GET /is_sleeping (server started with --enable-sleep-mode and
                   VLLM_SERVER_DEV_MODE=1). Level 1 keeps weights in pinned CPU memory: fastest wake.
  sglang           POST /release_memory_occupation, POST /resume_memory_occupation (--enable-memory-saver). Experimental.
  park-protocol    Berth's park protocol: GET /berth/state, POST /berth/park, POST /berth/resume. Anything that speaks it,
                   including `gaprunner adapter` sidecars.
  cuda-checkpoint  NVIDIA cuda-checkpoint on each pid: lock -> checkpoint (GPU memory to host), restore -> unlock.
                   Uses explicit --action steps, never blind --toggle, and remembers the pids it froze: a checkpointed
                   process has no GPU context, so it does not show up in nvidia-smi.
  command          Shell hooks (park_cmd / resume_cmd / state_cmd), llmux-style.
  restart          For jobs that resume from their own checkpoints (item-resume benchmarks, step-save training):
                   park = SIGTERM (then SIGKILL after timeout_s), resume = run the recipe's start_cmd again.
  governor         A job its app asks for (governor.py): resume = grant the app's reservation, park = revoke it.
"""
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

from .scheduler import HOT, PARKED, PARKING, RESUMING, UNKNOWN


class DriverError(RuntimeError):
    pass


def http(method, url, body=None, headers=None, timeout=60):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data if method != "GET" else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip()[:1] in (b"{", b"[") else raw.decode(errors="replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def alive(pid):
    """True for a running process; False for a missing or zombie (exited, not yet reaped) one."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:                     # no /proc (macOS): ask ps
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
    except OSError:
        return True


def start_time(pid):
    """The kernel's start time for pid (clock ticks since boot), or None where /proc isn't available."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def leaders(spec):
    """Live pids named by a pid_file (lines "pid" or "pid start_time"); with a start time, a pid that now belongs to
    a different process (pid reuse) is not counted."""
    if spec.get("pid_file") and os.path.exists(os.path.expanduser(spec["pid_file"])):
        out = []
        with open(os.path.expanduser(spec["pid_file"])) as f:
            for line in f:
                parts = line.split()
                if not parts or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                if not alive(pid):
                    continue
                if len(parts) > 1 and parts[1].isdigit() and start_time(pid) not in (None, int(parts[1])):
                    continue
                out.append(pid)
        return out
    return []


def group_members(leader_pids):
    out = set()
    for pg in leader_pids:
        r = subprocess.run(["pgrep", "-g", str(pg)], capture_output=True, text=True)
        out.update(int(p) for p in r.stdout.split())
    return out


def pids_from(spec):
    """Every process of a job GapRunner launched (its process group: the launcher's wrapper names the group), plus
    what the recipe's own discovery finds; see _discover."""
    if spec.get("wrapper_pid_file"):
        # a job GapRunner launches is its launcher's process group; the recipe's own discovery (pgrep, pids_cmd)
        # only adds processes while that launcher runs (e.g. a container it started), so a name match on some
        # unrelated command can never make the job look running or receive its signals
        lead = leaders({"pid_file": spec["wrapper_pid_file"]})
        if not lead:
            return []
        found = set(lead) | group_members(lead)
        if spec.get("pid_file") != spec["wrapper_pid_file"]:
            found |= set(_discover(spec))
        return sorted(p for p in found if alive(p))
    return sorted(p for p in set(_discover(spec)) if alive(p))


_CACHE = {}
CACHE_S = 2.0          # process discovery by command (pids_cmd, pgrep) is reused for this long


def _cached(key, fn):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


def _discover(spec):
    """Live pids from {"pid_file"}, {"pids"}, {"pids_cmd"} (a shell command printing pids) or {"pgrep"}. For a job GapRunner started
    (spec["group"]), every process in its process group, so GPU and CPU memory are attributed to the job."""
    if spec.get("pid_file") and os.path.exists(os.path.expanduser(spec["pid_file"])):
        lead = leaders(spec)
        if spec.get("group") and lead:
            return sorted(set(lead) | group_members(lead))
        return lead
    if spec.get("pids"):
        return [p for p in (int(x) for x in spec["pids"]) if alive(p)]
    if spec.get("pids_cmd"):                     # e.g. "sudo -n docker top vllm -eo pid | tail -n +2"
        def run():
            r = subprocess.run(["sh", "-c", spec["pids_cmd"]], capture_output=True, text=True, timeout=10)
            return [int(p) for p in r.stdout.split() if p.isdigit()]
        return _cached(("cmd", spec["pids_cmd"]), run)
    if spec.get("pgrep"):
        def run():
            out = subprocess.run(["pgrep", "-f", spec["pgrep"]], capture_output=True, text=True).stdout.split()
            return [int(p) for p in out if int(p) != os.getpid()]
        return _cached(("pgrep", spec["pgrep"]), run)
    return []


class Driver:
    def __init__(self, spec):
        self.spec = spec
        self.timeout = float(spec.get("timeout_s", 120))

    def state(self):
        return UNKNOWN

    def park(self):
        raise NotImplementedError

    def resume(self):
        raise NotImplementedError

    def pids(self):
        return pids_from(self.spec)

    def signal(self, sig):
        """Send sig to the job: its whole process group when GapRunner started it, else each of its pids."""
        wrapper = leaders({"pid_file": self.spec["wrapper_pid_file"]}) if self.spec.get("wrapper_pid_file") else []
        if wrapper:                                   # a job we launched: signal its whole process group
            targets, send = wrapper, os.killpg
        elif self.spec.get("group"):
            targets, send = leaders(self.spec), os.killpg
        else:
            targets, send = self.pids(), os.kill
        for pid in targets:
            try:
                send(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def pause(self):
        """Stop computing but keep GPU memory (SIGSTOP); used by coexist = "yield" jobs while a service is busy."""
        self.signal(signal.SIGSTOP)

    def unpause(self):
        self.signal(signal.SIGCONT)

    def kill(self):
        self.signal(signal.SIGKILL)

    def _wait_state(self, want, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state() == want:
                return
            time.sleep(0.1)
        raise DriverError(f"{self.spec.get('name')}: not {want} after {timeout:.0f}s (state {self.state()})")


def _timed_out(e):
    """True when an HTTP call failed by timing out (the server is up but slow), not by being refused or reset."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


class Vllm(Driver):
    #: A server busy with long model steps can answer /is_sleeping slowly. Within this long of the last real answer,
    #: a timed-out check keeps that answer instead of reporting UNKNOWN (which would send a request down the wake
    #: path for a server that is already awake). A refused connection or an error status is still UNKNOWN at once.
    STALE_OK_S = 120.0
    STATE_TIMEOUT_S = 5.0

    def __init__(self, spec):
        super().__init__(spec)
        self.url = spec["url"].rstrip("/")
        self.level = int(spec.get("sleep_level", 1))
        self.headers = spec.get("headers", {})
        self._last = None                     # (state, monotonic time) of the last real answer

    def state(self):
        try:
            code, v = http("GET", self.url + "/is_sleeping", headers=self.headers, timeout=self.STATE_TIMEOUT_S)
        except OSError as e:
            if _timed_out(e) and self._last and time.monotonic() - self._last[1] <= self.STALE_OK_S:
                return self._last[0]
            return UNKNOWN
        if code != 200:
            return UNKNOWN
        asleep = v.get("is_sleeping") if isinstance(v, dict) else str(v).strip().lower() == "true"
        st = PARKED if asleep else HOT
        self._last = (st, time.monotonic())
        return st

    def park(self):
        code, v = http("POST", f"{self.url}/sleep?level={self.level}", body=b"", headers=self.headers, timeout=self.timeout)
        if code not in (200, 204):
            raise DriverError(f"sleep -> {code} {str(v)[:200]}")
        self._wait_state(PARKED, 10)

    def resume(self):
        code, v = http("POST", self.url + "/wake_up", body=b"", headers=self.headers, timeout=self.timeout)
        if code not in (200, 204):
            raise DriverError(f"wake_up -> {code} {str(v)[:200]}")
        self._wait_state(HOT, self.timeout)


class Sglang(Driver):
    """SGLang has no is-sleeping endpoint, so state is what GapRunner last did (UNKNOWN until the first transition)."""

    def __init__(self, spec):
        super().__init__(spec)
        self.url = spec["url"].rstrip("/")
        self._state = UNKNOWN

    def state(self):
        return self._state

    def park(self):
        code, v = http("POST", self.url + "/release_memory_occupation", body={}, timeout=self.timeout)
        if code != 200:
            raise DriverError(f"release_memory_occupation -> {code} {str(v)[:200]}")
        self._state = PARKED

    def resume(self):
        code, v = http("POST", self.url + "/resume_memory_occupation", body={}, timeout=self.timeout)
        if code != 200:
            raise DriverError(f"resume_memory_occupation -> {code} {str(v)[:200]}")
        self._state = HOT


class ParkProtocol(Driver):
    """Berth's park protocol (github.com/T-Crypt/berth, contrib/PARK-PROTOCOL.md)."""

    def __init__(self, spec):
        super().__init__(spec)
        self.url = spec["url"].rstrip("/")

    def state(self):
        try:
            code, v = http("GET", self.url + "/berth/state", timeout=3)
        except OSError:
            return UNKNOWN
        st = v.get("state") if isinstance(v, dict) else None
        return st if code == 200 and st in (HOT, PARKED, PARKING, RESUMING) else UNKNOWN

    def park(self):
        code, v = http("POST", self.url + "/berth/park", body=b"", timeout=self.timeout)
        if code != 200:
            raise DriverError(f"park -> {code} {str(v)[:200]}")

    def resume(self):
        code, v = http("POST", self.url + "/berth/resume", body=b"", timeout=self.timeout)
        if code != 200:
            raise DriverError(f"resume -> {code} {str(v)[:200]}")


class CudaCheckpoint(Driver):
    STATES = {"running": HOT, "checkpointed": PARKED, "locked": PARKING}

    def __init__(self, spec):
        super().__init__(spec)
        self.bin = spec.get("cuda_checkpoint", "cuda-checkpoint")
        self.sudo = ["sudo", "-n"] if spec.get("sudo") else []
        self.frozen = []               # pids we checkpointed; they vanish from nvidia-smi, so never re-discover them

    def _run(self, *args):
        r = subprocess.run(self.sudo + [self.bin, *args], capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise DriverError(f"{' '.join(args)}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout.strip()

    def _pid_state(self, pid):
        try:
            return self.STATES.get(self._run("--get-state", "--pid", str(pid)), UNKNOWN)
        except (DriverError, subprocess.TimeoutExpired):
            return UNKNOWN

    def pids(self):
        return sorted(set(pids_from(self.spec)) | set(self.frozen))

    def state(self):
        states = {self._pid_state(p) for p in self.pids()}
        return states.pop() if len(states) == 1 else UNKNOWN

    def park(self):
        pids = self.pids()
        if not pids:
            raise DriverError("no pids to checkpoint")
        for pid in pids:
            st = self._pid_state(pid)
            if st == HOT:
                self._run("--action", "lock", "--pid", str(pid), "--timeout", str(int(self.timeout * 1000)))
                st = PARKING
            if st == PARKING:
                self._run("--action", "checkpoint", "--pid", str(pid))
            if pid not in self.frozen:
                self.frozen.append(pid)
        if self.state() != PARKED:
            raise DriverError(f"after checkpoint state is {self.state()}")

    def resume(self):
        for pid in self.pids():
            if self._pid_state(pid) == PARKED:
                self._run("--action", "restore", "--pid", str(pid))
            if self._pid_state(pid) == PARKING:
                self._run("--action", "unlock", "--pid", str(pid))
        if self.state() != HOT:
            raise DriverError(f"after restore state is {self.state()}")
        self.frozen = []


class Command(Driver):
    def __init__(self, spec):
        super().__init__(spec)

    def _sh(self, key):
        cmd = self.spec.get(key)
        if not cmd:
            raise DriverError(f"no {key} configured")
        env = {**os.environ, **self.spec.get("env", {}), "GAPRUNNER_NAME": self.spec.get("name", "")}
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=self.timeout, env=env)
        if r.returncode != 0:
            raise DriverError(f"{key} exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout.strip()

    def state(self):
        if not self.spec.get("state_cmd"):
            return UNKNOWN
        try:
            out = self._sh("state_cmd").split()
        except (DriverError, subprocess.TimeoutExpired):
            return UNKNOWN
        return out[0] if out and out[0] in (HOT, PARKED, PARKING, RESUMING) else UNKNOWN

    def park(self):
        self._sh("park_cmd")

    def resume(self):
        self._sh("resume_cmd")


class Restart(Driver):
    def state(self):
        return HOT if self.pids() else PARKED

    def park(self):
        """SIGTERM the job, then SIGKILL whatever of it is still alive after timeout_s. The pids are taken up front,
        because the launcher may exit first and leave children running."""
        pids = self.pids()
        self.signal(signal.SIGTERM)
        deadline = time.monotonic() + self.timeout
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.signal(signal.SIGKILL)
        for p in pids:
            if alive(p):
                try:
                    os.kill(p, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        deadline = time.monotonic() + 10
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        if any(alive(p) for p in pids):
            raise DriverError(f"processes still alive after SIGKILL: {[p for p in pids if alive(p)]}")

    def resume(self):
        raise DriverError("restart jobs are resumed by running start_cmd again")


KINDS = {"vllm": Vllm, "sglang": Sglang, "park-protocol": ParkProtocol, "cuda-checkpoint": CudaCheckpoint,
         "command": Command, "restart": Restart}


def make(spec):
    kind = spec.get("driver")
    if kind == "governor":                        # a job an app asks for (governor.py)
        from .governor import GovernorJob
        return GovernorJob(spec)
    if kind not in KINDS:
        raise ValueError(f"{spec.get('name')}: driver must be one of {', '.join([*KINDS, 'governor'])}")
    return KINDS[kind](spec)
