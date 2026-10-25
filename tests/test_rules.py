"""CPU-memory floor, dependencies, drain, claims, max_hours, GPU hold and restart settling."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from test_scheduler import LONG, POL, job, kinds, snap
from gaprunner import config as C
from gaprunner.runner import Runner
from gaprunner.scheduler import PARKED, Policy, decide, explain

PY = sys.executable


class SchedulerRulesTest(unittest.TestCase):
    def test_cpu_floor_blocks_admission_and_parking(self):
        pol = Policy(**{**POL.__dict__, "cpu_floor_gib": 16})
        s = snap(svc_state=PARKED, jobs=[job(gib=30, run_cpu_gib=40)])
        s.cpu_free_gib = 50                                        # 50 - 40 < 16
        self.assertEqual(decide(s, LONG, pol), [])
        self.assertIn("host memory", explain(s, LONG, pol)["bench"][1])
        s.cpu_free_gib = 60
        self.assertEqual(kinds(decide(s, LONG, pol)), [("resume_job", "bench")])
        hot = snap(jobs=[job()])                                    # prod would take 200 GiB of host RAM to park
        hot.services[0].park_cpu_gib, hot.cpu_free_gib = 200, 150
        self.assertEqual(decide(hot, LONG, pol), [])

    def test_dependency_blocks_until_done(self):
        s = snap(svc_state=PARKED, jobs=[job(blocked_by="waiting for train-a to finish")])
        self.assertEqual(decide(s, LONG, POL), [])
        self.assertEqual(explain(s, LONG, POL)["bench"], ("blocked", "waiting for train-a to finish"))

    def test_hold_reasons_stop_new_starts_and_parks(self):
        s = snap(jobs=[job()])
        s.hold = "draining: running jobs finish, nothing new starts"
        self.assertEqual(decide(s, LONG, POL), [])                  # no park_service, no starts
        self.assertEqual(explain(snap(svc_state=PARKED, jobs=[job()]), LONG, POL)["bench"][0], "next")


class RunnerRulesTest(unittest.TestCase):
    def build(self, jobs, claims=(), **top):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        d = tempfile.mkdtemp()
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": d, **top}, "policy": {"reserve_gib": 0},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                    "gpu_gib": 20}], "job": list(jobs), "claim": list(claims)})
        r = Runner(cfg, gpu=gpu)
        r.host_mem = lambda: None
        self.addCleanup(lambda: [e["driver"].kill() for e in r.job.values()])
        return r

    def ticks(self, r, n=10, gap=0.05):
        for _ in range(n):
            r.tick()
            time.sleep(gap)

    def test_after_runs_in_order_and_failure_blocks(self):
        quick = {"driver": "restart", "gpu_gib": 5, "coexist": "share", "park_s": 0.1}
        r = self.build([{"name": "a", "start_cmd": f"{PY} -c 'pass'", **quick},
                        {"name": "b", "after": "a", "start_cmd": f"{PY} -c 'pass'", **quick},
                        {"name": "c", "start_cmd": f"{PY} -c 'raise SystemExit(2)'", **quick},
                        {"name": "d", "after": ["c"], "start_cmd": f"{PY} -c 'pass'", **quick}])
        self.ticks(r, 40)
        self.assertEqual(r.job["a"]["state"], "done")
        self.assertEqual(r.job["b"]["state"], "done")
        self.assertEqual(r.job["c"]["state"], "failed")
        self.assertEqual(r.job["d"]["state"], "parked")
        self.assertIn("c failed", r.status()["jobs"]["d"]["why"])

    def test_claim_hold_and_track(self):
        marker = f"gaprunner-claim-test-{os.getpid()}"
        proc = subprocess.Popen([PY, "-c", "import time,sys; time.sleep(30)", marker])
        self.addCleanup(proc.kill)
        r = self.build([{"name": "j", "driver": "restart", "gpu_gib": 5, "coexist": "share", "park_s": 0.1,
                         "start_cmd": f"{PY} -c 'import time; time.sleep(30)'"}],
                       claims=[{"name": "hand-experiment", "action": "hold", "pgrep": marker, "linger_s": 0.2}])
        self.ticks(r, 5)
        self.assertEqual(r.job["j"]["state"], "parked")              # held off while the hand-run work runs
        self.assertIn("claim hand-experiment", r.status()["jobs"]["j"]["why"])
        proc.kill()
        proc.wait()
        r.claims_at = 0
        self.ticks(r, 12)
        self.assertEqual(r.job["j"]["state"], "hot")

    def test_drain_and_settle(self):
        r = self.build([{"name": "j", "driver": "restart", "gpu_gib": 5, "coexist": "share", "park_s": 0.1,
                         "start_cmd": f"{PY} -c 'import time; time.sleep(30)'"}])
        open(r.drain_path, "w").close()
        self.ticks(r, 4)
        self.assertEqual(r.job["j"]["state"], "parked")
        os.remove(r.drain_path)
        r.settle_until = r.clock() + 60                               # long enough for any machine's ticks
        self.ticks(r, 4)
        self.assertEqual(r.job["j"]["state"], "parked")              # still settling after a restart
        r.settle_until = r.clock()                                   # settling over
        self.ticks(r, 12)
        self.assertEqual(r.job["j"]["state"], "hot")

    def test_max_hours_parks_and_holds(self):
        r = self.build([{"name": "j", "driver": "restart", "gpu_gib": 5, "coexist": "share", "park_s": 0.1,
                         "max_hours": 0.5 / 3600, "start_cmd": f"{PY} -c 'import time; time.sleep(30)'"}])
        self.ticks(r, 30)
        self.assertEqual(r.job["j"]["state"], "parked")
        self.assertTrue(C.queue_overrides(r.top)["j"]["hold"])
        self.assertIn("max_hours", [e["event"] for e in r.recent])

    def test_gpu_hold_blocks_until_cleared(self):
        r = self.build([{"name": "j", "driver": "restart", "gpu_gib": 5, "coexist": "share", "park_s": 0.1,
                         "start_cmd": f"{PY} -c 'import time; time.sleep(30)'"}])
        holder = subprocess.Popen(["sleep", "30"])
        with open(r.gpu_hold_path, "w") as f:
            f.write(f'{{"job": "x", "pids": [{holder.pid}]}}')
        self.ticks(r, 4)
        self.assertEqual(r.job["j"]["state"], "parked")
        holder.kill()
        holder.wait()
        self.ticks(r, 12)
        self.assertFalse(os.path.exists(r.gpu_hold_path))            # cleared once those processes are gone
        self.assertEqual(r.job["j"]["state"], "hot")


if __name__ == "__main__":
    unittest.main()
