"""When a job's frozen copy doesn't fit in host memory, GapRunner first stops frozen jobs lower in the queue (last first)
so the job can still step aside without losing work. A job higher in the queue is never stopped for a lower one."""
import sys
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner.runner import Runner
from gaprunner.scheduler import PARKED

PY = sys.executable
SLEEP = f"{PY} -c 'import time; time.sleep(60)'"


class HostEvictTest(unittest.TestCase):
    def build(self):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        mk = lambda name, parked_cpu: {"name": name, "driver": "restart", "gpu_gib": 10, "coexist": "share",
                                       "park_s": 0.1, "timeout_s": 5, "start_cmd": SLEEP,
                                       "running": {"cpu_gib": 10}, "parked": {"cpu_gib": parked_cpu}}
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
                       "policy": {"reserve_gib": 0, "cpu_floor_gib": 16},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                    "gpu_gib": 20}],
                       "job": [mk("high", 50), mk("low", 30)]})          # queue order: high first, low last
        r = Runner(cfg, gpu=gpu)
        self.addCleanup(lambda: [e["driver"].kill() for e in r.job.values()])
        r.refresh()
        for n in ("high", "low"):
            self.assertTrue(r._start(n, reason="test"))
        for _ in range(40):
            if all(r.job[n]["driver"].pids() for n in ("high", "low")):
                break
            time.sleep(0.05)
        return r

    def test_the_lowest_job_is_stopped_so_a_higher_one_can_freeze(self):
        r = self.build()
        r.job["low"]["state"] = PARKED                           # frozen: parked with its processes alive
        reads = iter([30.0, 100.0])                              # 30 - 40 extra < 16: short; after the stop, room
        r.host_mem = lambda: next(reads, 100.0)
        self.assertTrue(r._park_job("high", reason="test"))
        ev = [e for e in r.recent_events() if e.get("event") in ("evicted", "park")]
        self.assertEqual([(e["event"], e["target"]) for e in ev][::-1][:2], [("evicted", "low"), ("park", "high")])
        self.assertFalse(r.job["low"]["driver"].pids())          # low's processes are gone; it restarts later
        self.assertNotEqual(ev[0].get("how"), "stopped")         # high stepped aside normally, not by the fallback

    def test_a_higher_job_is_never_stopped_for_a_lower_one(self):
        r = self.build()
        r.job["high"]["state"] = PARKED
        r.host_mem = lambda: 30.0
        self.assertEqual(r._evict_for_host("low", 50.0), 0.0)
        self.assertTrue(r.job["high"]["driver"].pids())
        self.assertFalse([e for e in r.recent_events() if e.get("event") == "evicted"])


if __name__ == "__main__":
    unittest.main()
