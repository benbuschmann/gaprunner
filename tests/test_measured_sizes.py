"""Measured sizes beside the recipe's: the host memory a park really takes (a recipe that understates it must not let
host memory run out), each run's GPU peak, and a job paused by hand not holding a max_active_jobs slot."""
import json
import sys
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from test_scheduler import LONG, POL, job, kinds, snap
from gaprunner import config as C
from gaprunner.runner import Runner
from gaprunner.scheduler import HOT, PARKED, Policy, decide, explain

PY = sys.executable
SLEEP = f"{PY} -c 'import time; time.sleep(60)'"
ONE = Policy(**{**POL.__dict__, "max_active_jobs": 1})


class FrozenSlotTest(unittest.TestCase):
    def test_a_paused_job_frees_its_slot(self):
        s = snap(svc_state=PARKED, jobs=[job("a", state=HOT, order=0, frozen=True), job("b", gib=10, order=1)])
        self.assertEqual(kinds(decide(s, LONG, ONE)), [("resume_job", "b")])

    def test_a_paused_job_still_holds_its_gpu_memory(self):
        s = snap(svc_state=PARKED, jobs=[job("a", state=HOT, gib=90, order=0, frozen=True), job("b", gib=40, order=1)])
        self.assertEqual(decide(s, LONG, ONE), [])
        self.assertEqual(explain(s, LONG, ONE)["b"][0], "waiting")

    def test_a_running_job_still_holds_its_slot(self):
        s = snap(svc_state=PARKED, jobs=[job("a", state=HOT, order=0), job("b", gib=10, order=1)])
        self.assertEqual(decide(s, LONG, ONE), [])


class MeasuredHostTest(unittest.TestCase):
    def build(self, parked_cpu=10):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
                       "policy": {"reserve_gib": 0, "cpu_floor_gib": 16},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                    "gpu_gib": 20, "running": {"cpu_gib": 0}, "parked": {"cpu_gib": parked_cpu}}],
                       "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "park_s": 0.1,
                                "timeout_s": 5, "start_cmd": SLEEP}]})
        r = Runner(cfg, gpu=gpu)
        self.addCleanup(lambda: [e["driver"].kill() for e in r.job.values()])
        r.refresh()
        return r

    def test_a_park_records_the_host_memory_it_took(self):
        r = self.build()
        reads = iter([300.0, 130.0])                 # MemAvailable before and after the service's park
        r.host_mem = lambda: next(reads, 130.0)
        self.assertTrue(r._transition(r.svc["prod"], "prod", "park", "test"))
        self.assertEqual(r.costs.get(r.svc["prod"]["cost_key"], "park_host"), (170.0, True))
        with open(r.audit_path) as f:
            ev = [json.loads(x) for x in f if '"park"' in x][-1]
        self.assertEqual(ev["host_gib"], 170.0)

    def test_the_larger_of_recipe_and_measurement_decides(self):
        r = self.build(parked_cpu=41)
        key = r.svc["prod"]["cost_key"]
        self.assertEqual(r.host_park_gib(key, 41), 41)                  # nothing measured yet: the recipe
        r.costs.record(key, "park_host", 170)
        self.assertEqual(r.host_park_gib(key, 41), 170)                 # the recipe understates it
        self.assertEqual(r.host_park_gib(key, 200), 200)                # never below the recipe
        r.host_mem = lambda: 160.0
        self.assertEqual([s.park_cpu_gib for s in r.snapshot().services], [170])

    def test_the_service_doesnt_sleep_when_its_measured_cost_doesnt_fit(self):
        # 160 GiB available: the recipe's 41 GiB would fit, the measured 170 GiB would leave host memory below the floor
        s = snap(jobs=[job("a", order=0)])
        s.services[0].park_cpu_gib, s.cpu_free_gib = 41.0, 160.0
        self.assertIn(("park_service", "prod"), kinds(decide(s, LONG, POL)))
        s.services[0].park_cpu_gib = 170.0
        self.assertNotIn(("park_service", "prod"), kinds(decide(s, LONG, POL)))


class GpuPeakTest(unittest.TestCase):
    def test_each_runs_peak_is_kept(self):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                    "gpu_gib": 20}],
                       "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "park_s": 0.1, "timeout_s": 5,
                                "start_cmd": SLEEP}]})
        r = Runner(cfg, gpu=gpu)
        self.addCleanup(lambda: [e["driver"].kill() for e in r.job.values()])
        r.refresh()
        self.assertTrue(r._start("j", reason="test"))
        for _ in range(40):
            if r.job["j"]["driver"].pids():
                break
            time.sleep(0.05)
        pid = r.job["j"]["driver"].pids()[0]
        r.job["j"]["state"] = HOT
        for g in (12.0, 31.5, 20.0):
            r.gpu.read = lambda fresh=False, g=g: {"total_gib": 100.0, "used_gib": g, "procs": [{"pid": pid, "gib": g, "name": "x"}]}
            r.sample()
        self.assertEqual(r.job["j"]["gpu_peak_now"], 31.5)
        self.assertIsNone(r.gpu_peak_gib("j"))
        r._park_job("j", reason="test")
        self.assertEqual(r.gpu_peak_gib("j"), 31.5)
        self.assertNotIn("gpu_peak_now", r.job["j"])


if __name__ == "__main__":
    unittest.main()
