"""One service, switchable engines: the gate follows the current engine, the old one is stopped, jobs can require an
engine and their own traffic keeps the service awake without counting as its activity."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import http.client

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import gate
from gaprunner.runner import Runner

PY = sys.executable


def call(port, path="/v1/chat/completions", model="prod"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=json.dumps({"model": model, "stream": True}).encode(), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


class EngineTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.down = {n: os.path.join(d, f"{n}-down") for n in ("glm", "qwen")}
        open(self.down["qwen"], "w").close()                                # qwen starts out stopped
        gpu = FakeGpu(100.0)
        down = self.down

        class Gpu(FakeGpu):
            def read(self, fresh=False):
                for n in ("glm", "qwen"):
                    if os.path.exists(down[n]):
                        self.resident.pop(n, None)
                return super().read(fresh)
        self.gpu = Gpu(100.0)
        self.glm = FakeVllm(self.gpu, "glm", 70.0, down_file=self.down["glm"])
        self.qwen = FakeVllm(self.gpu, "qwen", 30.0, down_file=self.down["qwen"])
        self.bodies = []
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0},
            "service": [{"name": "prod", "listen": "127.0.0.1:0", "idle_park_s": 600, "engines": [
                {"name": "glm", "driver": "vllm", "upstream": self.glm.url, "gpu_gib": 70,
                 "stop_cmd": f"touch {self.down['glm']}", "start_cmd": f"rm -f {self.down['glm']}"},
                {"name": "qwen", "driver": "vllm", "upstream": self.qwen.url, "gpu_gib": 30, "model_name": "qwen-27b",
                 "stop_cmd": f"touch {self.down['qwen']}", "start_cmd": f"rm -f {self.down['qwen']}"}]}],
            "job": [{"name": "bench", "driver": "restart", "gpu_gib": 0, "coexist": "share", "park_s": 0.1,
                     "requires_engine": "qwen", "start_cmd": f"{PY} -c 'import os,time; print(os.environ[\"GAPRUNNER_URL\"], os.environ[\"GAPRUNNER_ENGINE\"], flush=True); time.sleep(60)'"}],
        })
        self.r = Runner(cfg, gpu=self.gpu)
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(self.r, "prod"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.port = srv.server_address[1]
        self.stop = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()
        self.addCleanup(self.stop.set)
        self.addCleanup(lambda: self.r.job["bench"]["driver"].kill())

    def loop(self):
        while not self.stop.is_set():
            self.r.tick()
            time.sleep(0.05)

    def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_switch_stops_old_engine_and_gate_follows(self):
        self.assertEqual(call(self.port), 200)
        self.assertEqual(self.r.status()["jobs"]["bench"]["word"], "blocked")      # needs qwen
        self.r.switch_engine("prod", "qwen")
        self.assertTrue(self.wait_for(lambda: self.r.svc["prod"]["engine"] == "qwen"))
        self.assertTrue(os.path.exists(self.down["glm"]))                           # old engine stopped, not asleep
        self.assertEqual(call(self.port), 200)                                      # the gate now goes to qwen
        self.assertIn("/v1/chat/completions", self.qwen.calls)
        self.assertTrue(self.wait_for(lambda: self.r.job["bench"]["state"] == "hot"))   # the qwen-only job starts
        log = os.path.join(self.r.top["state_dir"], "job-bench.log")
        self.assertTrue(self.wait_for(lambda: os.path.exists(log) and "_job/bench qwen" in open(log).read()))
        # its traffic keeps prod awake but is not prod activity
        n = len(self.r.svc["prod"]["gaps"])
        self.assertEqual(call(self.port, "/_job/bench/v1/chat/completions"), 200)
        self.assertEqual(len(self.r.svc["prod"]["gaps"]), n)
        # switch back: the qwen-only job is parked (it no longer has its engine)
        self.r.switch_engine("prod", "glm")
        self.assertTrue(self.wait_for(lambda: self.r.svc["prod"]["engine"] == "glm"))
        self.assertTrue(self.wait_for(lambda: self.r.job["bench"]["state"] == "parked"))
        self.assertEqual(json.load(open(self.r._engine_path("prod")))["engine"], "glm")

    def test_a_failed_switch_stops_the_new_engine_before_restoring_the_old(self):
        # qwen "starts" but never answers: the switch times out, qwen is stopped, glm comes back
        self.r.svc["prod"]["engines"]["qwen"]["spec"]["ready_timeout_s"] = 1
        self.r.svc["prod"]["engines"]["qwen"]["spec"]["start_cmd"] = "true"         # starts nothing: stays down
        self.r.switch_engine("prod", "qwen")
        self.assertTrue(self.wait_for(lambda: self.r.svc["prod"]["switching"] is None, 15))
        self.assertEqual(self.r.svc["prod"]["engine"], "glm")
        ev = [e["event"] for e in self.r.recent]
        self.assertIn("engine_switch_failed", ev)
        self.assertGreaterEqual(sum(1 for e in self.r.recent if e["event"] == "service_stop"), 2)   # glm, then qwen
        self.assertTrue(self.wait_for(lambda: self.r.svc["prod"]["state"] == "hot", 15))

    def test_requests_during_a_switch_get_a_clear_503(self):
        self.r.svc["prod"]["switching"] = {"to": "qwen", "since": time.time(), "reason": "test"}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/v1/chat/completions", body=b"{}", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 503)
        self.assertIn(b"switching to qwen", r.read())


if __name__ == "__main__":
    unittest.main()


class ScheduleTest(unittest.TestCase):
    def test_switches_once_per_slot_and_hand_switch_holds(self):
        import datetime as dt
        d = tempfile.mkdtemp()
        cfg = C.parse({"gaprunner": {"state_dir": d},
                       "service": [{"name": "prod", "listen": "127.0.0.1:0", "schedule": [{"at": "23:00", "engine": "qwen"},
                                                                                         {"at": "07:00", "engine": "glm"}],
                                    "engines": [{"name": "glm", "driver": "vllm", "upstream": "http://127.0.0.1:9", "gpu_gib": 70,
                                                 "stop_cmd": "true", "start_cmd": "true"},
                                                {"name": "qwen", "driver": "vllm", "upstream": "http://127.0.0.1:9", "gpu_gib": 30,
                                                 "stop_cmd": "true", "start_cmd": "true"}]}]})
        now = [dt.datetime(2026, 10, 23, 22, 58).timestamp()]
        r = Runner(cfg, gpu=FakeGpu(100), clock=lambda: now[0])
        calls = []
        r.switch_engine = lambda n, e, reason="": calls.append(e)
        r._schedules()                                   # first look: remembers the current slot, does nothing
        now[0] = dt.datetime(2026, 10, 23, 23, 0).timestamp()
        r._schedules()
        r._schedules()
        self.assertEqual(calls, ["qwen"])                # once
        r.svc["prod"]["engine"] = "glm"                  # someone switched back by hand
        now[0] = dt.datetime(2026, 10, 24, 2, 0).timestamp()
        r._schedules()
        self.assertEqual(calls, ["qwen"])                # the hand switch holds until the next slot
        now[0] = dt.datetime(2026, 10, 24, 7, 0).timestamp()
        r.svc["prod"]["engine"] = "qwen"
        r._schedules()
        self.assertEqual(calls, ["qwen", "glm"])
