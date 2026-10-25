"""Sleep when host RAM allows, else stop; release-on-demand; the watchdog's bypass."""
import os
import sys
import tempfile
import threading
import time
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import gate, watchdog
from gaprunner.runner import Runner

PY = sys.executable


def runner(job, svc_extra=None, host=lambda: None):
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", 20.0)
    d = tempfile.mkdtemp()
    cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": d}, "policy": {"reserve_gib": 0, "cpu_floor_gib": 16},
                   "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                "gpu_gib": 20, **(svc_extra or {})}], "job": [job]})
    r = Runner(cfg, gpu=gpu)
    r.host_mem = host
    return r


class FallbackTest(unittest.TestCase):
    def test_stops_instead_of_sleeping_when_host_ram_is_short(self):
        d = tempfile.mkdtemp()
        froze = os.path.join(d, "froze")
        job = {"name": "j", "driver": "command", "gpu_gib": 10, "coexist": "share", "fallback": "restart",
               "start_cmd": f"{PY} -c 'import time; time.sleep(60)'", "park_cmd": f"touch {froze}",
               "resume_cmd": "true", "running": {"hbm_gib": 10, "cpu_gib": 5}, "parked": {"cpu_gib": 200}}
        r = runner(job, host=lambda: 100.0)                        # 100 free - 195 extra < 16: can't sleep
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        r.tick()
        self.assertTrue(r._start("j"))
        time.sleep(0.3)
        with r.lock:
            r._park_job("j", reason="test")
        self.assertFalse(os.path.exists(froze))                    # the sleep hook was not used
        self.assertEqual(r.job["j"]["driver"].pids(), [])          # it was stopped
        park = [e for e in r.recent if e["event"] == "park"][-1]
        self.assertEqual(park["how"], "stopped")
        r.host_mem = lambda: 400.0                                 # plenty of room: it sleeps
        self.assertTrue(r._start("j"))
        time.sleep(0.3)
        with r.lock:
            r._park_job("j", reason="test")
        self.assertTrue(os.path.exists(froze))

    def test_release_runs_once_when_a_job_is_short_of_host_ram(self):
        d = tempfile.mkdtemp()
        rel = os.path.join(d, "released")
        job = {"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "park_s": 0.1,
               "start_cmd": f"{PY} -c 'import time; time.sleep(60)'", "running": {"hbm_gib": 10, "cpu_gib": 50}}
        r = runner(job, svc_extra={"release_cmd": f"echo x >> {rel}"}, host=lambda: 40.0)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        for _ in range(4):
            r.tick()
        self.assertEqual(open(rel).read().count("x"), 1)           # once per wake, not every tick
        self.assertEqual(r.job["j"]["state"], "parked")            # still short: it waits
        self.assertTrue(r.svc["prod"]["released"])


class ParkStopsOnlyTheJobTest(unittest.TestCase):
    def test_a_job_stopped_by_its_park_cmd_is_not_failed(self):
        # like a hook that stops only the job's own process: the wrapper lives on and records exit 143
        d = tempfile.mkdtemp()
        pid = os.path.join(d, "pid")
        job = {"name": "j", "driver": "command", "gpu_gib": 10, "coexist": "share",
               "start_cmd": f"{PY} -c 'import os, time; open(\"{pid}\", \"w\").write(str(os.getpid())); time.sleep(60)'",
               "park_cmd": f"kill $(cat {pid})", "resume_cmd": "true"}
        r = runner(job)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        r.tick()
        self.assertTrue(r._start("j"))
        for _ in range(50):
            if os.path.exists(pid):
                break
            time.sleep(0.1)
        with r.lock:
            r._park_job("j", reason="test")
        for _ in range(50):
            r.refresh()
            if r.job["j"]["proc"] is None:
                break
            time.sleep(0.1)
        r.refresh()
        self.assertIsNone(r.job["j"]["result"])
        self.assertFalse(os.path.exists(r._result_path("j")))
        exit_ = [e for e in r.recent if e["event"] == "job_exit"][-1]
        self.assertIn("stopped by park", exit_["note"])


class WatchdogTest(unittest.TestCase):
    def test_bypass_after_misses(self):
        d = tempfile.mkdtemp()
        flag = os.path.join(d, "bypassed")
        cfg = C.parse({"gaprunner": {"state_dir": d, "watchdog_every_s": 0.05, "watchdog_failures": 3},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": "http://127.0.0.1:9",
                                    "listen": "127.0.0.1:9", "gpu_gib": 20, "bypass_cmd": f"touch {flag}"}]})
        st = watchdog.run(cfg, rounds=5)
        self.assertTrue(os.path.exists(flag))
        self.assertTrue(st["prod"]["bypassed"])
        self.assertIn("bypass_cmd exit 0", open(os.path.join(d, "watchdog.log")).read())


if __name__ == "__main__":
    unittest.main()
