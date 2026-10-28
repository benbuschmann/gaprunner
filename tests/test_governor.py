"""driver = "governor": jobs an app asks for. The app side is the real admission client, run as its own process."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import fakes  # noqa: F401 -- test settings (no host-memory reading, immediate process discovery)
from test_scheduler import LONG, POL, job, kinds, snap
from gaprunner import config as C
from gaprunner import governor as GV
from gaprunner.gpu import FakeGpu
from gaprunner.scheduler import HOT, IDLE, PARKED, PARKING, Policy, decide, explain

LINUX = os.path.exists("/proc/self/stat")
ONE = Policy(**{**POL.__dict__, "max_active_jobs": 1})


class StubRunner:
    def __init__(self, folder, live=True):
        self.top = {"governor_dir": folder, "governor_uids": [], "state_dir": folder}
        self.live, self.gpu, self.events, self.parked, self.closed = live, FakeGpu(100.0), [], [], []
        self.policy, self.drain_path = POL, os.path.join(folder, "DRAIN")
        self.window_only, self.window_open, self.hold = False, False, False

    def paused(self):
        return False

    def snapshot(self):
        from gaprunner.scheduler import Job, Snapshot
        j = Job("WASS video", IDLE, 70, 1, 1, 0, hold=self.hold, window_only=self.window_only)
        return Snapshot(1000, [], [j], 100, window_open=self.window_open)

    def audit(self, event, **kw):
        self.events.append((event, kw))

    def _close_gpu_peak(self, name):
        self.closed.append(name)

    def _park_job(self, name, reason=""):
        self.parked.append((name, reason))


def spec(**kw):
    return {"name": "WASS video", "driver": "governor", "owner": "studio-comfy", "timeout_s": 10, "grace_s": 2,
            "admit_wait_s": 30, **kw}


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


class GovernorConfigTest(unittest.TestCase):
    base = {"service": [{"name": "prod", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:0", "gpu_gib": 1}]}

    def test_defaults_and_rules(self):
        cfg = C.parse({**self.base, "job": [{"name": "v", "driver": "governor", "owner": "o", "gpu_gib": 70}]})
        j = cfg["jobs"][0]
        self.assertTrue(j["loses_work"])
        self.assertEqual((j["admit_wait_s"], j["grace_s"]), (300.0, 30.0))
        self.assertTrue(cfg["gaprunner"]["governor_dir"].endswith("governor"))
        with self.assertRaises(C.ConfigError):        # the app starts it
            C.parse({**self.base, "job": [{"name": "v", "driver": "governor", "owner": "o", "gpu_gib": 1, "start_cmd": "x"}]})
        with self.assertRaises(C.ConfigError):        # who asks?
            C.parse({**self.base, "job": [{"name": "v", "driver": "governor", "gpu_gib": 1}]})
        with self.assertRaises(C.ConfigError):
            C.parse({**self.base, "job": [{"name": "a", "driver": "governor", "owner": "o", "gpu_gib": 1},
                                          {"name": "b", "driver": "governor", "owner": "o", "gpu_gib": 1}]})
        with self.assertRaises(C.ConfigError):
            C.parse({**self.base, "gaprunner": {"governor_uids": ["studio"]}})


class GovernorSchedulingTest(unittest.TestCase):
    def test_an_idle_job_takes_no_place(self):
        s = snap(svc_state=PARKED, jobs=[job("wass", state=IDLE, order=0), job("train", order=1)])
        self.assertEqual(kinds(decide(s, LONG, ONE)), [("resume_job", "train")])
        self.assertEqual(explain(s, LONG, ONE)["wass"][0], "idle")

    def test_a_service_wake_also_takes_a_job_still_stepping_aside(self):
        from gaprunner.scheduler import plan_wake
        s = snap(svc_state=PARKED, jobs=[job("wass", state=PARKING, coexist="share", gib=10, order=0)])
        self.assertEqual(plan_wake(s, "prod"), ["wass"])

    def test_a_request_takes_its_place_at_the_top(self):
        # the app asked: the job waits in its place and pushes the training job (lower, keeps its work) aside
        s = snap(svc_state=PARKED, jobs=[job("wass", order=0, loses_work=True), job("train", state=HOT, order=1)])
        acts = decide(s, LONG, ONE)
        self.assertEqual(kinds(acts), [("park_job", "train")])
        self.assertIn("makes room for wass", acts[0].reason)


class OrphanedGroupTest(unittest.TestCase):
    def test_children_are_found_after_the_wrapper_exits(self):
        from gaprunner import drivers as D
        p = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; exit 0"], stdout=subprocess.PIPE, text=True,
                             start_new_session=True)
        child = int(p.stdout.readline())
        p.wait()                                                         # the group leader is gone
        d = tempfile.mkdtemp()
        pid_file = os.path.join(d, "job.pid")
        with open(pid_file, "w") as f:
            f.write(f"{p.pid}\n")
        try:
            self.assertIn(child, D.pids_from({"wrapper_pid_file": pid_file, "pid_file": pid_file, "group": True}))
        finally:
            os.kill(child, 9)
        time.sleep(0.3)
        # the whole group is gone: nothing is found, not even by the recipe's own discovery
        spec2 = {"wrapper_pid_file": pid_file, "pid_file": os.path.join(d, "other"), "pgrep": "sleep"}
        keep = subprocess.Popen(["sleep", "30"])
        try:
            self.assertEqual(D.pids_from(spec2), [])
        finally:
            keep.kill()
            keep.wait()


class LinesTest(unittest.TestCase):
    def test_two_messages_sent_together_are_both_read(self):
        import socket as so, importlib.util
        spec_ = importlib.util.spec_from_file_location("ac", os.path.join(os.path.dirname(GV.__file__), "governor_client",
                                                                           "admission_client.py"))
        ac = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(ac)
        a, b = so.socketpair()
        a.sendall(b'{"allowed": true, "token": "t"}\n{"op": "revoke"}\n')
        lines = ac.Lines(b)
        self.assertTrue(lines.read()["allowed"])
        self.assertTrue(lines.ready(0.1))                # the revoke is not stuck in a buffer select() can't see
        self.assertEqual(lines.read()["op"], "revoke")
        a.close(); b.close()


class ShapeTest(unittest.TestCase):
    def test_shape_of_a_prompt(self):
        prompt = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "fasth3_8step.safetensors"}},
                  "2": {"class_type": "EmptyLatentVideo", "inputs": {"width": 864, "height": 480, "length": 124}},
                  "3": {"class_type": "KSampler", "inputs": {"steps": 8, "cfg": 1.0, "seed": 7, "model": ["1", 0]}},
                  "4": {"class_type": "LoraLoader", "inputs": {"lora_name": "horror80s.safetensors", "strength": 1.0}}}
        out = GV.shape_of(prompt)
        self.assertEqual(out["shape"], {"width": 864, "height": 480, "length": 124, "steps": 8, "cfg": 1.0})
        self.assertEqual(out["models"], ["fasth3_8step.safetensors", "horror80s.safetensors"])
        self.assertEqual(out["nodes"]["KSampler"], 1)


@unittest.skipUnless(LINUX, "process sessions are read from /proc")
class GovernorProtocolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gr-gov-")
        self.runner = StubRunner(self.dir)
        self.job = GV.GovernorJob(spec())
        self.gov = GV.Governor(self.runner, [self.job])
        self.gov.start()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stderr.close()
        self.gov.stop()

    def launch(self, *cmd, owner="studio-comfy"):
        p = subprocess.Popen([sys.executable, os.path.join(self.dir, "admission_client.py"), "--owner", owner,
                              "--workload", "h3", "--socket", self.gov.sock_path, "--estimated-incremental-gib", "85",
                              "--run", *cmd], stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def rpc(self, req):
        sys.path.insert(0, self.dir)
        import admission_client
        return admission_client.rpc(req, self.gov.sock_path)

    def rows(self):
        with open(os.path.join(self.dir, "state", "reservations.json")) as f:
            return json.load(f)["reservations"]

    def test_the_folder_holds_what_the_app_imports(self):
        for name in GV.CLIENT_FILES:
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)))
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.rpc({"op": "preflight", "owner": "studio-comfy"})["allowed"])
        self.assertFalse(self.rpc({"op": "preflight", "owner": "someone"})["allowed"])

    def test_ask_wait_grant_release(self):
        p = self.launch("sleep", "1")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))       # waiting in the queue, not running
        self.assertEqual(self.rows(), [])
        self.job.resume()                                                    # the scheduler admits it
        self.assertEqual(self.job.state(), HOT)
        self.assertTrue(wait_for(lambda: self.rows() and self.rows()[0].get("binding")))
        row = self.rows()[0]
        self.assertEqual(row["owner"], "studio-comfy")
        self.assertTrue(self.job.pids())
        # what the app checks before each submission: its reservation's session holds its server
        self.assertTrue(set(self.job.pids()) & set(GV.PB.session(row["binding"])))
        self.assertEqual(p.wait(10), 0)
        self.assertTrue(wait_for(lambda: self.job.state() == IDLE))
        self.assertTrue(wait_for(lambda: self.rows() == []))
        self.gov.tick()
        names = [e for e, _ in self.runner.events]
        self.assertIn("governor_request", names)
        self.assertIn("governor_grant", names)
        self.assertIn("governor_release", names)

    def test_revoke_stops_the_command(self):
        p = self.launch("sleep", "60")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertTrue(wait_for(lambda: self.job.req is not None and self.job.req.binding is not None))
        t0 = time.monotonic()
        self.job.park()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.job.state(), IDLE)
        self.assertNotEqual(p.wait(10), 0)                                  # the command was stopped, not finished
        self.assertTrue(wait_for(lambda: self.rows() == []))

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(self):
        ready = os.path.join(self.dir, "trapped")
        p = self.launch("sh", "-c", f"trap '' TERM; touch {ready}; sleep 60")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertTrue(wait_for(lambda: self.job.req is not None and self.job.req.binding is not None))
        self.assertTrue(wait_for(lambda: os.path.exists(ready)))      # it ignores SIGTERM from here on
        t0 = time.monotonic()
        self.job.park()
        self.assertGreaterEqual(time.monotonic() - t0, 1.5)                  # grace_s = 2
        self.assertEqual(self.job.state(), IDLE)
        p.wait(10)

    def test_one_reservation_at_a_time(self):
        self.launch("sleep", "30")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        second = self.launch("true")
        self.assertEqual(second.wait(10), 3)
        self.assertIn("already has", second.stderr.read())

    def test_a_request_nobody_admits_is_refused_in_time(self):
        self.job.spec["admit_wait_s"] = 0.5
        p = self.launch("true")
        self.assertEqual(p.wait(10), 3)
        self.assertIn("did not make room", p.stderr.read())
        self.assertEqual(self.job.state(), IDLE)
        self.assertFalse(self.rpc({"op": "status", "owner": "studio-comfy"})["allowed"])   # don't ask again at once

    def test_a_request_that_cannot_run_soon_is_turned_down_at_once(self):
        self.runner.window_only = True                                    # window-only, and no window is open
        t0 = time.monotonic()
        p = self.launch("true")
        self.assertEqual(p.wait(10), 3)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("not now: windows only", p.stderr.read())
        self.assertFalse(self.rpc({"op": "status", "owner": "studio-comfy"})["allowed"])
        self.runner.window_only, self.runner.hold = False, True          # wound down
        p = self.launch("true")
        self.assertEqual(p.wait(10), 3)
        self.assertIn("wound down", p.stderr.read())
        self.runner.hold, self.runner.window_open, self.runner.window_only = False, True, True
        p = self.launch("sleep", "0.2")                                  # a window is open: it waits in the queue
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertEqual(p.wait(10), 0)

    def test_a_binding_must_be_the_launcher_s_own_session(self):
        self.assertFalse(GV.owned_by({"sid": os.getsid(0), "leader": os.getpid(), "uid": os.getuid()},
                                     {"uid": os.getuid() + 1, "pid": None}))           # another user's peer
        self.assertFalse(GV.owned_by({"sid": 1, "leader": 1, "uid": 0}, {"uid": os.getuid(), "pid": None}))
        self.assertFalse(GV.owned_by("x", {"uid": os.getuid()}))
        # a forged rebind with a token GapRunner never issued is refused
        out = self.rpc({"op": "rebind", "owner": "studio-comfy", "token": "made-up",
                        "binding": {"sid": os.getsid(0), "leader": os.getpid(), "uid": os.getuid()}})
        self.assertFalse(out["allowed"])
        self.assertEqual(self.job.state(), IDLE)

    def test_the_folder_must_not_be_a_link(self):
        import tempfile
        d = tempfile.mkdtemp()
        os.symlink(tempfile.mkdtemp(), os.path.join(d, "gov"))
        r = StubRunner(os.path.join(d, "gov"))
        with self.assertRaises(RuntimeError):
            GV.Governor(r, [GV.GovernorJob(spec())]).install()

    def test_grant_returns_at_once_and_an_unbound_grant_expires(self):
        import socket as so
        c = so.socket(so.AF_UNIX)
        c.connect(self.gov.sock_path)
        c.sendall(b'{"op": "reserve", "owner": "studio-comfy"}\n')       # asks, and never binds a command
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.spec["bind_timeout_s"] = 0.3
        t0 = time.monotonic()
        self.job.resume()
        self.assertLess(time.monotonic() - t0, 1.0)                      # no waiting under the scheduler's lock
        f = c.makefile("r")
        self.assertTrue(json.loads(f.readline())["allowed"])
        time.sleep(0.4)
        self.gov.tick()
        self.assertEqual(json.loads(f.readline())["op"], "revoke")
        self.assertEqual(self.job.state(), PARKING)                     # not forgotten while it might still start
        self.job.spec["grace_s"] = 0.01
        time.sleep(0.2)
        self.assertEqual(self.job.state(), PARKING)                     # however long its launcher stays connected
        f.close()
        c.close()
        self.assertTrue(wait_for(lambda: self.job.state() == IDLE))       # its launcher is gone: nothing left

    def test_a_bind_after_a_revoke_is_tracked_and_revoked_again(self):
        import socket as so
        c = so.socket(so.AF_UNIX)
        c.connect(self.gov.sock_path)
        c.sendall(b'{"op": "reserve", "owner": "studio-comfy"}\n')
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.spec["bind_timeout_s"] = 0.2
        self.job.resume()
        f = c.makefile("r")
        token = json.loads(f.readline())["token"]
        time.sleep(0.3)
        self.gov.tick()                                                   # revoked before any bind
        self.assertEqual(json.loads(f.readline())["op"], "revoke")
        child = subprocess.Popen(["sleep", "30"], start_new_session=True) # the launcher started it anyway
        try:
            from gaprunner.governor_client import process_binding as pb
            c.sendall((json.dumps({"op": "bind", "token": token, "binding": pb.binding(child.pid)}) + "\n").encode())
            self.assertEqual(json.loads(f.readline())["op"], "revoke")    # told again
            self.assertEqual(self.job.state(), PARKING)                  # and its command is counted
        finally:
            child.kill()
            child.wait()
            c.close()
        self.assertTrue(wait_for(lambda: self.job.state() == IDLE))

    def test_the_command_dies_with_its_launcher(self):
        p = self.launch("sleep", "60")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertTrue(wait_for(lambda: self.job.pids()))
        p.kill()                                                         # SIGKILL: no clean-up in the launcher
        p.wait()
        self.assertTrue(wait_for(lambda: self.job.state() == IDLE, 10))  # the kernel stopped the command too

    def test_a_restart_that_doesnt_know_the_token_stops_the_command(self):
        p = self.launch("sleep", "60")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertTrue(wait_for(lambda: self.job.req is not None and self.job.req.binding is not None))
        self.gov.stop()
        os.remove(self.gov.tokens_path)                                  # a GapRunner that never issued it
        job2 = GV.GovernorJob(spec())
        gov2 = GV.Governor(self.runner, [job2])
        gov2.start()
        try:
            self.assertNotEqual(p.wait(15), 0)                           # the launcher stopped its command
            self.assertEqual(job2.state(), IDLE)
        finally:
            gov2.stop()

    def test_unknown_owner(self):
        p = self.launch("true", owner="nobody")
        self.assertEqual(p.wait(10), 3)

    def test_a_killed_launcher_leaves_the_reservation_until_its_session_ends(self):
        # the command ignores the SIGTERM the kernel sends when its launcher dies: GapRunner keeps counting it
        ready = os.path.join(self.dir, "trapped2")
        p = self.launch("sh", "-c", f"trap '' TERM; touch {ready}; sleep 2")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertTrue(wait_for(lambda: os.path.exists(ready)))
        self.assertTrue(wait_for(lambda: self.job.pids()))
        pids = self.job.pids()
        p.kill()
        p.wait()
        self.assertEqual(self.job.state(), HOT)                              # the command still holds the GPU
        self.assertTrue(wait_for(lambda: self.job.state() == IDLE, 10))      # ... until it ends
        self.assertFalse(any(os.path.exists(f"/proc/{x}") and GV.PB.process(x)["state"] != "Z" for x in pids))

    def test_a_restarted_gaprunner_takes_the_reservation_back(self):
        p = self.launch("sleep", "30")
        self.assertTrue(wait_for(lambda: self.job.state() == PARKED))
        self.job.resume()
        self.assertTrue(wait_for(lambda: self.job.req is not None and self.job.req.binding is not None))
        self.gov.stop()                                                      # GapRunner restarts
        job2 = GV.GovernorJob(spec())
        gov2 = GV.Governor(self.runner, [job2])
        gov2.start()
        try:
            self.assertTrue(wait_for(lambda: job2.state() == HOT, 15))
            self.assertEqual(self.rows()[0]["owner"], "studio-comfy")
            job2.park()
            self.assertNotEqual(p.wait(10), 0)
        finally:
            gov2.stop()

    def test_shadow_mode_admits_at_once(self):
        self.runner.live = False
        p = self.launch("sleep", "0.5")
        self.assertEqual(p.wait(10), 0)
        self.assertIn("governor_shadow_grant", [e for e, _ in self.runner.events])


class FakeComfy:
    def __init__(self):
        self.running, self.history = [], {}
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/queue":
                    body = {"queue_running": [[0, pid, {}, {}, []] for pid in fake.running], "queue_pending": []}
                elif self.path.startswith("/history/"):
                    pid = self.path.rsplit("/", 1)[1]
                    body = {pid: fake.history[pid]} if pid in fake.history else {}
                else:
                    body = {}
                d = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class RecorderTest(unittest.TestCase):
    def test_one_row_per_prompt_and_idle_release(self):
        d = tempfile.mkdtemp(prefix="gr-rec-")
        comfy = FakeComfy()
        runner = StubRunner(d)
        j = GV.GovernorJob(spec(comfy_url=comfy.url, idle_release_s=0.3))
        gov = GV.Governor(runner, [j])
        r = GV.Request("tok", {}, None, {})
        r.granted_at, r.binding = time.time(), None
        r.granted.set()
        j.req = r
        rec = gov.recorders["WASS video"]
        rec.poll()                                                   # ComfyUI answers: "ready"
        comfy.running = ["p1"]
        rec.poll()
        comfy.running = []
        comfy.history["p1"] = {"prompt": [0, "p1", {"2": {"class_type": "EmptyLatentVideo",
                                                         "inputs": {"width": 1376, "height": 768, "length": 243}}}],
                               "status": {"status_str": "success", "messages": [
                                   ["execution_start", {"timestamp": 1000}], ["execution_success", {"timestamp": 494000}]]}}
        rec.poll()
        with open(rec.path) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([x["event"] for x in rows], ["ready", "prompt"])
        self.assertEqual(rows[1]["shape"], {"width": 1376, "height": 768, "length": 243})
        self.assertEqual(rows[1]["seconds"], 493.0)
        self.assertEqual(rows[1]["status"], "success")
        time.sleep(0.4)
        gov.tick()                                                   # idle longer than idle_release_s: revoked
        self.assertEqual(runner.parked[0][0], "WASS video")
        comfy.running = ["p2"]                                       # a prompt cut off by the end of the reservation
        rec.poll()
        j.req = None
        rec.poll()
        with open(rec.path) as f:
            last = [json.loads(x) for x in f][-1]
        self.assertEqual((last["prompt_id"], last["status"]), ("p2", "interrupted"))
        comfy.server.shutdown()


if __name__ == "__main__":
    unittest.main()
