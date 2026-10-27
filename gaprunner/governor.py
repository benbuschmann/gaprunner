"""Jobs that ask first: GapRunner as the workload governor of an app that requests GPU time before it starts.

Some apps start their own GPU server when a user asks for work (Imagination Studio's renderer is one). They can't be
started from a recipe; instead they ask a governor for a reservation and wait. GapRunner serves that request as a job
in its queue (driver "governor"):

  idle      nothing asked for it; it holds no place on the GPU
  waiting   the app asked (op "reserve"); the job waits in the queue like any other, in its place
  running   GapRunner admitted it: the reservation is granted and the app's command runs in a session of its own
  stepping aside  GapRunner revokes the reservation (op "revoke"); the app's command gets SIGTERM, then SIGKILL

The app side is three small modules GapRunner copies into the governor folder (governor_client/): admission_client
(the --run launcher and rpc), process_binding and governor (machine readings). The folder also holds
state/reservations.json and the admission socket state/admission.sock.

Protocol: one JSON object per line over the Unix socket. Requests: preflight, reserve, bind, rebind, release, status.
GapRunner sends revoke on a reserve connection. Only uids listed in governor_uids (and GapRunner's own) may connect;
the check uses SO_PEERCRED, so a socket open to other users needs Linux.
"""
import collections
import json
import os
import shutil
import socket
import struct
import threading
import time
import uuid
from importlib import resources

from . import drivers as D
from .scheduler import HOT, IDLE, PARKED, PARKING

CLIENT_FILES = ("admission_client.py", "process_binding.py", "governor.py")
HISTORY = 50


def _client():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gaprunner_process_binding", str(resources.files("gaprunner").joinpath("governor_client", "process_binding.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PB = _client()          # the same process_binding the app uses: both sides read a session the same way


class Request:
    def __init__(self, token, msg, conn, peer):
        self.token, self.msg, self.conn, self.peer = token, msg, conn, peer
        self.asked_at, self.granted_at, self.revoked_at = time.time(), None, None
        self.granted, self.bound = threading.Event(), threading.Event()
        self.binding, self.answered = None, False
        self.send_lock = threading.Lock()

    def send(self, obj):
        with self.send_lock:
            self.conn.sendall((json.dumps(obj) + "\n").encode())


class GovernorJob(D.Driver):
    """The driver of a job an app asks for. One reservation at a time per job (per owner name)."""

    def __init__(self, spec):
        super().__init__(spec)
        self.owner = spec["owner"]
        self.lock = threading.RLock()
        self.req = None                   # the current Request: waiting or granted
        self.on_change = lambda: None     # set by the Governor: rewrite reservations.json
        self.events = collections.deque(maxlen=HISTORY)
        self.refused_at = 0.0

    # ---- the driver interface ----
    def state(self):
        ended = False
        with self.lock:
            r = self.req
            if r is None:
                return IDLE
            if r.revoked_at is not None:
                return PARKING if self._alive(r) else IDLE
            if not r.granted.is_set():
                return PARKED                    # waiting in the queue
            if r.binding is None or self._alive(r):
                return HOT                       # granted (and the app may still be starting its command)
            self.req, ended = None, True         # its session ended without a release (the launcher was killed)
        if ended:
            self._event("ended", note="the session ended without a release")
            self.on_change()
        return IDLE

    def pids(self):
        with self.lock:
            r = self.req
        if r is None or r.binding is None:
            return []
        return D._cached(("governor", r.token), lambda: PB.session(r.binding))

    def resume(self):
        """Admit the waiting request: answer it with a token, then wait for the app to start its command."""
        with self.lock:
            r = self.req
            if r is None or r.granted.is_set():
                raise D.DriverError(f"{self.spec['name']}: nothing is waiting for a reservation")
            r.granted_at = time.time()
            r.granted.set()
        try:
            r.send({"allowed": True, "token": r.token, "grace_s": float(self.spec.get("grace_s", 30))})
            r.answered = True
        except OSError as e:
            self._drop(r, "the app went away before it was admitted")
            raise D.DriverError(f"{self.spec['name']}: the app went away before it was admitted ({e})") from None
        if not r.bound.wait(self.timeout):
            raise D.DriverError(f"{self.spec['name']}: the app was admitted but did not start within {self.timeout:.0f}s")
        self._event("grant", wait_s=round(r.granted_at - r.asked_at, 1))

    def park(self):
        """Revoke the reservation: the app's command is stopped; whatever it was doing is lost."""
        with self.lock:
            r = self.req
            if r is None:
                return
            if not r.granted.is_set():
                self._refuse(r, "GapRunner took the request out of the queue")
                return
            first = r.revoked_at is None
            if first:
                r.revoked_at = time.time()
        if first:
            try:
                r.send({"op": "revoke", "grace_s": float(self.spec.get("grace_s", 30))})
            except OSError:
                pass                              # the launcher is gone; its session ends with it or is waited out
        deadline = time.monotonic() + self.timeout
        while self._alive(r) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._alive(r):
            raise D.DriverError(f"{self.spec['name']}: still running {self.timeout:.0f}s after its reservation was revoked")
        self._event("revoke", seconds=round(time.time() - r.revoked_at, 1))
        self._drop(r, None)

    # ---- requests from the socket ----
    def ask(self, msg, conn, peer):
        with self.lock:
            if self.req is not None:
                return None, f"{self.spec['name']} already has a reservation or a request in the queue"
            r = Request(uuid.uuid4().hex, msg, conn, peer)
            self.req = r
        return r, None

    def bind(self, token, bound):
        with self.lock:
            r = self.req
            if r is None or r.token != token or not r.granted.is_set():
                return False
            r.binding = bound
        r.bound.set()
        self.on_change()
        return True

    def adopt(self, msg, conn, peer):
        """A launcher whose GapRunner restarted: take back its live reservation."""
        bound = msg.get("binding")
        if not bound or not PB.session(bound):
            return False, "that session has ended"
        with self.lock:
            if self.req is not None and self.req.token != msg.get("token"):
                return False, f"{self.spec['name']} already has another reservation"
            r = Request(msg["token"], msg, conn, peer)
            r.granted_at, r.binding, r.answered = time.time(), bound, True
            r.granted.set()
            r.bound.set()
            self.req = r
        self.on_change()
        self._event("adopt")
        return True, None

    def release(self, token):
        with self.lock:
            r = self.req
            if r is None or r.token != token:
                return False
        held = round(time.time() - (r.granted_at or r.asked_at), 1)
        self._drop(r, None)
        self._event("release", held_s=held)
        return True

    def gone(self, r):
        """The reserve connection closed. Before a grant that ends the request; after one, the session decides."""
        with self.lock:
            if self.req is not r:
                return
            if not r.granted.is_set():
                self.req = None
                self._event("withdrawn")
            elif r.binding is None or not PB.session(r.binding):
                self.req = None
        self.on_change()

    def expire(self, r, wait_s):
        """A request that waited longer than the app will wait: refuse it so the app can say why."""
        with self.lock:
            if self.req is not r or r.granted.is_set():
                return
        self._refuse(r, f"GapRunner did not make room within {wait_s:.0f} s")

    def reservations(self):
        with self.lock:
            r = self.req
            if r is None or not r.granted.is_set():
                return []
            return [{"owner": self.owner, "token": r.token, "workload": r.msg.get("workload"),
                     "gib": r.msg.get("estimated_incremental_gib"), "binding": r.binding,
                     "granted_at": round(r.granted_at or 0, 1)}]

    def waiting_since(self):
        with self.lock:
            r = self.req
            return r.asked_at if r is not None and not r.granted.is_set() else None

    # ---- helpers ----
    def _alive(self, r):
        return bool(r.binding and PB.session(r.binding))

    def _refuse(self, r, why):
        with self.lock:
            if self.req is r:
                self.req = None
            self.refused_at = time.time()
        try:
            r.send({"allowed": False, "reason": why})
        except OSError:
            pass
        self._event("refuse", reason=why)
        self.on_change()

    def _drop(self, r, why):
        with self.lock:
            if self.req is r:
                self.req = None
        self.on_change()

    def _event(self, what, **kw):
        self.events.append({"t": round(time.time(), 1), "event": what, **kw})


class Governor:
    """Serves the admission socket for every governor job, and keeps the folder the app reads."""

    def __init__(self, runner, jobs):
        top = runner.top
        self.runner, self.folder = runner, os.path.expanduser(top["governor_dir"])
        self.uids = {int(u) for u in top.get("governor_uids", [])} | {os.getuid()}
        self.by_owner = {j.owner: j for j in jobs}
        self.state_dir = os.path.join(self.folder, "state")
        self.sock_path = os.path.join(self.state_dir, "admission.sock")
        self.lock = threading.Lock()
        self.server, self.conns = None, set()
        self.recorders = {j.spec["name"]: Recorder(runner, j) for j in jobs if j.spec.get("comfy_url")}
        self.was_hot = {}
        for j in jobs:
            j.on_change = self.write_reservations

    def install(self):
        """Create the folder the app reads: the client modules (refreshed from this GapRunner), state/ and an empty
        reservations.json. Refuses a folder another user owns."""
        os.makedirs(self.state_dir, mode=0o755, exist_ok=True)
        for p in (self.folder, self.state_dir):
            if os.stat(p).st_uid != os.getuid():
                raise RuntimeError(f"{p} belongs to another user; GapRunner keeps its governor folder to itself")
            os.chmod(p, 0o755)
        src = resources.files("gaprunner").joinpath("governor_client")
        for name in CLIENT_FILES:
            dst = os.path.join(self.folder, name)
            tmp = f"{dst}.{os.getpid()}.tmp"
            with resources.as_file(src.joinpath(name)) as f:
                shutil.copyfile(f, tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
        self.write_reservations()

    def write_reservations(self):
        path = os.path.join(self.state_dir, "reservations.json")
        with self.lock:                       # read and write under one lock: a slower writer never puts back old rows
            rows = [row for j in self.by_owner.values() for row in j.reservations()]
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"     # never another writer's temporary file
            with open(tmp, "w") as f:
                json.dump({"reservations": rows, "t": round(time.time(), 1)}, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)

    def start(self):
        self.install()
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.sock_path)
        others = self.uids - {os.getuid()}
        if others and not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("governor_uids needs Linux (SO_PEERCRED) to check who connects")
        os.chmod(self.sock_path, 0o666 if others else 0o600)
        s.listen(16)
        self.server = s
        threading.Thread(target=self._accept, daemon=True, name="governor").start()
        if self.recorders:
            threading.Thread(target=self._record_forever, daemon=True, name="governor-record").start()

    def _record_forever(self):
        while self.server is not None:
            for rec in self.recorders.values():
                try:
                    rec.poll()
                except Exception as e:  # noqa: BLE001 -- measuring must never get in the way
                    self.runner.audit("governor_record_error", target=rec.job.spec["name"], error=str(e)[:200])
            time.sleep(2.0)

    def tick(self):
        """Under the runner's lock, once a tick: copy each job's events to the audit log, close a run's GPU peak when
        its reservation ends, and revoke a reservation whose app has been idle for idle_release_s."""
        for j in self.by_owner.values():
            name = j.spec["name"]
            while j.events:
                ev = j.events.popleft()
                self.runner.audit(f"governor_{ev.pop('event')}", target=name, **{k: v for k, v in ev.items() if k != "t"})
            hot = j.state() == HOT
            if self.was_hot.get(name) and not hot:
                self.runner._close_gpu_peak(name)
            self.was_hot[name] = hot
            idle_release = float(j.spec.get("idle_release_s", 600) or 0)
            rec = self.recorders.get(name)
            if hot and idle_release and rec is not None and self.runner.live:
                idle = rec.idle_s()
                if idle is not None and idle >= idle_release:
                    self.runner._park_job(name, f"its app has been idle for {idle / 60:.0f} min")

    def stop(self):
        srv, self.server = self.server, None
        if srv is not None:
            try:
                srv.shutdown(socket.SHUT_RDWR)        # wakes the accept thread; a bare close() leaves it blocked
            except OSError:
                pass
            srv.close()
        for conn in list(self.conns):               # as when GapRunner exits: launchers see the socket close
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _accept(self):
        srv = self.server
        while self.server is srv and srv is not None:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            if self.server is not srv:                 # stopped meanwhile
                conn.close()
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True, name="governor-conn").start()

    def _peer(self, conn):
        if not hasattr(socket, "SO_PEERCRED"):
            return {"pid": None, "uid": os.getuid()}          # the socket is open to GapRunner's own user only
        pid, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return {"pid": pid, "uid": uid}

    def _serve(self, conn):
        f = conn.makefile("r")
        held = None                                            # a reserve request this connection carries
        self.conns.add(conn)
        try:
            peer = self._peer(conn)
            if peer["uid"] not in self.uids:
                conn.sendall((json.dumps({"allowed": False, "reason": f"uid {peer['uid']} may not ask GapRunner"}) + "\n").encode())
                return
            while True:
                line = f.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    conn.sendall(b'{"allowed": false, "reason": "not JSON"}\n')
                    return
                op = msg.get("op")
                if op == "reserve":
                    held = self._reserve(msg, conn, peer)
                    if held is None:
                        return
                elif op == "rebind":
                    held = self._rebind(msg, conn, peer)
                elif op == "bind":
                    if held is not None:
                        self.by_owner[held.msg["owner"]].bind(msg.get("token"), msg.get("binding"))
                elif op == "release":
                    job = self._job_of_token(msg.get("token"), msg.get("owner"))
                    ok = bool(job and job.release(msg.get("token")))
                    if held is None:                           # a one-shot rpc wants an answer
                        conn.sendall((json.dumps({"allowed": ok} if ok else
                                                 {"allowed": True, "reason": "no such reservation"}) + "\n").encode())
                        return
                else:
                    conn.sendall((json.dumps(self._answer(op, msg)) + "\n").encode())
                    return
        except OSError:
            return
        finally:
            self.conns.discard(conn)
            if held is not None:
                job = self.by_owner.get(held.msg.get("owner"))
                if job is not None:
                    job.gone(held)
            try:
                f.close()
                conn.close()
            except OSError:
                pass

    def _reserve(self, msg, conn, peer):
        job = self.by_owner.get(msg.get("owner"))
        if job is None:
            conn.sendall((json.dumps({"allowed": False, "reason": f"no GapRunner job asks for owner {msg.get('owner')!r}"}) + "\n").encode())
            return None
        not_now = self._not_now(job)
        if not_now:
            # it would only wait out admit_wait_s: say so now, so the app can render elsewhere at once
            job.refused_at = time.time()
            conn.sendall((json.dumps({"allowed": False, "reason": f"not now: {not_now}"}) + "\n").encode())
            self.runner.audit("governor_refuse", target=job.spec["name"], reason=not_now, pid=msg.get("pid"))
            return None
        r, why = job.ask(msg, conn, peer)
        if r is None:
            conn.sendall((json.dumps({"allowed": False, "reason": why}) + "\n").encode())
            return None
        self.runner.audit("governor_request", target=job.spec["name"], workload=msg.get("workload"),
                          gib=msg.get("estimated_incremental_gib"), pid=msg.get("pid"))
        wait_s = float(job.spec.get("admit_wait_s", 300))
        threading.Timer(wait_s, job.expire, args=(r, wait_s)).start()
        if not self.runner.live:
            # shadow mode schedules nothing: the app is admitted at once, and what it does is still measured
            threading.Thread(target=self._shadow_grant, args=(job,), daemon=True).start()
        return r

    def _shadow_grant(self, job):
        try:
            job.resume()
            self.runner.audit("governor_shadow_grant", target=job.spec["name"],
                              note="shadow mode: admitted at once; live mode would queue it in its place")
        except D.DriverError as e:
            self.runner.audit("governor_shadow_grant_failed", target=job.spec["name"], error=str(e)[:200])

    def _not_now(self, job):
        """Why a request for this job can't be admitted any time soon (wound down, turned off, window-only with no
        window open, the kill switch on, winding everything down), or None. Live mode only."""
        if not self.runner.live:
            return None
        from .scheduler import eligible
        name = job.spec["name"]
        if self.runner.paused():
            return "GapRunner's kill switch is on"
        if os.path.exists(self.runner.drain_path):
            return "GapRunner is winding everything down"
        snap = self.runner.snapshot()
        j = next((x for x in snap.jobs if x.name == name), None)
        if j is None:
            return None
        ok, why = eligible(snap, j, self.runner.policy)
        return None if ok else {"held": "the job is wound down", "disabled": "the job is turned off"}.get(why, why)

    def _rebind(self, msg, conn, peer):
        job = self.by_owner.get(msg.get("owner"))
        if job is None:
            conn.sendall(b'{"allowed": false, "reason": "no such job"}\n')
            return None
        ok, why = job.adopt(msg, conn, peer)
        conn.sendall((json.dumps({"allowed": ok, **({"reason": why} if why else {})}) + "\n").encode())
        if ok:
            self.runner.audit("governor_adopt", target=job.spec["name"])
            return job.req
        return None

    def _job_of_token(self, token, owner):
        for j in self.by_owner.values():
            with j.lock:
                if j.req is not None and j.req.token == token and (owner is None or owner == j.owner):
                    return j
        return None

    def _answer(self, op, msg):
        job = self.by_owner.get(msg.get("owner"))
        if op == "preflight":
            if job is None:
                return {"allowed": False, "detail": f"no GapRunner job asks for owner {msg.get('owner')!r}"}
            return {"allowed": True, "detail": "GapRunner queues the reservation in the job's place"}
        if op == "status":
            if job is None:
                return {"allowed": False, "reservations": []}
            rows = [{k: v for k, v in r.items() if k != "token"} for r in job.reservations()]
            # just refused: the app should give up rather than ask again at once
            return {"allowed": time.time() - job.refused_at > 60, "reservations": rows}
        return {"allowed": False, "reason": f"unknown op {op!r}"}


# ---------- measurements: what each piece of work looked like ----------

SHAPE_KEYS = ("width", "height", "length", "frames", "num_frames", "video_frames", "batch_size", "steps", "cfg",
              "seconds", "duration")


def shape_of(prompt):
    """A ComfyUI prompt (its node graph) in a few numbers and names: the sizes, lengths and steps its nodes ask for,
    the model files they load and how many nodes of each type it has."""
    nodes = prompt if isinstance(prompt, dict) else {}
    shape, models, kinds = {}, set(), collections.Counter()
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        kinds[node.get("class_type", "?")] += 1
        for k, v in (node.get("inputs") or {}).items():
            if k in SHAPE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                shape[k] = max(shape.get(k, v), v)
            elif k.endswith("_name") and isinstance(v, str) and "." in v:
                models.add(v)
    return {"shape": shape, "models": sorted(models), "nodes": dict(kinds.most_common(12))}


class Recorder:
    """While a governor job runs, watches its app's ComfyUI (job field comfy_url): one row per prompt it runs, with
    the prompt's shape, how long it took, and the GPU and host memory the job's processes held at the peak.
    Rows go to <state_dir>/governor-<job>.jsonl."""

    def __init__(self, runner, job, poll_s=2.0):
        self.runner, self.job, self.poll_s = runner, job, poll_s
        self.url = job.spec["comfy_url"].rstrip("/")
        self.path = os.path.join(runner.top["state_dir"], f"governor-{_slug(job.spec['name'])}.jsonl")
        self.current = None                  # {"id", "t0", "gpu", "host"} of the prompt running now
        self.last_busy = None                # when ComfyUI last had work (queue not empty)
        self.ready_at = None                 # when ComfyUI first answered in this reservation
        self.token = None

    def _get(self, path):
        code, body = D.http("GET", self.url + path, timeout=5)
        if code != 200 or not isinstance(body, (dict, list)):
            raise D.DriverError(f"{path}: {code}")
        return body

    def poll(self):
        with self.job.lock:
            r = self.job.req
            token = r.token if r is not None and r.granted.is_set() else None
            granted_at = r.granted_at if token else None
        if token != self.token:              # a new reservation (or none): start over
            self.token, self.current, self.ready_at = token, None, None
            self.last_busy = granted_at
        if token is None:
            return
        try:
            q = self._get("/queue")
        except Exception:  # noqa: BLE001 -- ComfyUI starting or gone: nothing to record yet
            return
        now = time.time()
        if self.ready_at is None:
            self.ready_at = now
            self.write({"event": "ready", "seconds": round(now - granted_at, 1),
                        "note": "from the grant to ComfyUI's first answer"})
        running = [item[1] for item in q.get("queue_running", []) if len(item) > 1]
        if running or q.get("queue_pending"):
            self.last_busy = now
        pids = self.job.pids()
        reading = self.runner.gpu.read() or {}
        gpu = round(sum(p["gib"] for p in reading.get("procs", []) if p["pid"] in pids), 2)
        host = round(_rss_gib(pids) or 0.0, 2)
        if self.current is not None and self.current["id"] not in running:
            self.finish(self.current)
            self.current = None
        if self.current is None and running:
            self.current = {"id": running[0], "t0": now, "gpu": gpu, "host": host}
        if self.current is not None:
            self.current["gpu"] = max(self.current["gpu"], gpu)
            self.current["host"] = max(self.current["host"], host)

    def finish(self, cur):
        row = {"event": "prompt", "prompt_id": cur["id"], "seconds": round(time.time() - cur["t0"], 1),
               "gpu_peak_gib": cur["gpu"], "host_peak_gib": cur["host"]}
        try:
            h = self._get(f"/history/{cur['id']}").get(cur["id"], {})
            row.update(shape_of((h.get("prompt") or [None, None, {}])[2]))
            st = h.get("status") or {}
            row["status"] = st.get("status_str")
            stamps = {m[0]: m[1].get("timestamp") for m in st.get("messages", []) if len(m) > 1 and isinstance(m[1], dict)}
            if stamps.get("execution_start"):
                end = next((stamps[k] for k in ("execution_success", "execution_error", "execution_interrupted")
                            if stamps.get(k)), None)
                if end:
                    row["seconds"] = round((end - stamps["execution_start"]) / 1000, 1)
        except Exception as e:  # noqa: BLE001 -- the timing and memory are still worth keeping
            row["history_error"] = str(e)[:200]
        self.write(row)

    def write(self, row):
        with open(self.path, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "job": self.job.spec["name"], **row}) + "\n")

    def idle_s(self):
        return None if self.last_busy is None or self.current is not None else time.time() - self.last_busy


def _slug(name):
    from .runner import _slug as slug
    return slug(name)


def _rss_gib(pids):
    from .runner import _rss_gib as rss
    return rss(pids)
