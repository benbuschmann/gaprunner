"""Jobs that lose unsaved work when they step aside (restart jobs): GapRunner learns how long they run between saved
points and gives them only quiet spells likely to last until the next one. Jobs that sleep or freeze are unaffected."""
import os
import sys
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from test_scheduler import LONG, POL, job, kinds, snap
from gaprunner import config as C
from gaprunner.gaps import GapModel
from gaprunner.runner import Runner
from gaprunner.scheduler import HOT, PARKED, decide, explain

PY = sys.executable
HOURLY = {"prod": GapModel([3600] * 100, min_samples=20)}     # every gap lasts an hour


class SchedulerTest(unittest.TestCase):
    def test_short_gaps_are_not_worth_a_lost_step(self):
        s = lambda **k: snap(svc_state=PARKED, **k)
        self.assertEqual(decide(s(jobs=[job(save_s=1800)]), LONG, POL), [])      # 10-min gaps, 30-min steps
        why = explain(s(jobs=[job(save_s=1800)]), LONG, POL)["bench"][1]
        self.assertIn("next saved point", why)
        self.assertEqual(kinds(decide(s(jobs=[job()]), LONG, POL)), [("resume_job", "bench")])  # no lost work: as before
        self.assertEqual(kinds(decide(s(jobs=[job(save_s=1800)]), HOURLY, POL)), [("resume_job", "bench")])
        self.assertEqual(kinds(decide(s(jobs=[job(save_s=1800)], window=True), LONG, POL)), [("resume_job", "bench")])

    def test_prod_stays_awake_when_only_a_losing_job_waits(self):
        self.assertEqual(decide(snap(jobs=[job(save_s=1800)]), LONG, POL), [])
        self.assertEqual(kinds(decide(snap(jobs=[job(save_s=1800)]), HOURLY, POL)), [("park_service", "prod")])

    def test_beside_a_sleeping_service_only_if_it_could_save(self):
        # 50 GiB fits beside sleeping prod but not beside awake prod (100 - 4 - 60 = 36): it would step aside at the
        # next wake, so a losing job waits for a long enough gap; a job that sleeps starts at once
        s = lambda j: snap(svc_state=PARKED, jobs=[j])
        self.assertEqual(decide(s(job(gib=50, coexist="share", save_s=1800)), LONG, POL), [])
        self.assertEqual(kinds(decide(s(job(gib=50, coexist="share")), LONG, POL)), [("resume_job", "bench")])
        # one that fits beside awake prod never steps aside for it: no lost work, it starts
        self.assertEqual(kinds(decide(s(job(gib=20, coexist="share", save_s=1800)), LONG, POL)), [("resume_job", "bench")])


class ConfigTest(unittest.TestCase):
    def parse(self, **j):
        return C.parse({"service": [{"name": "prod", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:0",
                                     "gpu_gib": 1}],
                        "job": [{"name": "j", "gpu_gib": 1, "start_cmd": "true", **j}]})["jobs"][0]

    def test_defaults_by_driver(self):
        self.assertTrue(self.parse(driver="restart")["loses_work"])
        self.assertFalse(self.parse(driver="cuda-checkpoint", pgrep="x")["loses_work"])
        self.assertFalse(self.parse(driver="restart", loses_work=False)["loses_work"])
        with self.assertRaises(C.ConfigError):
            self.parse(driver="restart", save_every_s=0)


class RunnerTest(unittest.TestCase):
    def build(self):
        d = tempfile.mkdtemp()
        self.saved = os.path.join(d, "step")
        with open(self.saved, "w") as f:
            f.write("1\n")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": d}, "policy": {"reserve_gib": 0},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                    "gpu_gib": 20}],
                       "job": [{"name": "j", "driver": "restart", "gpu_gib": 5, "coexist": "share", "park_s": 0.1,
                                "start_cmd": f"{PY} -c 'import time; time.sleep(30)'", "saved_cmd": f"cat {self.saved}",
                                "save_every_s": 1800}]})
        r = Runner(cfg, gpu=gpu)
        r.host_mem = lambda: None
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        return r

    def test_learns_the_time_between_saved_points(self):
        r = self.build()
        j = r.job["j"]
        self.assertEqual(r.costs.get("j", "save"), (1800.0, False))           # the recipe's guess until measured
        r._track_saves("j", j, True, 100.0)                                    # first look: where it is
        j["save_run_s"], j["run_s"] = 0.0, 1500.0                              # started from its saved point
        with open(self.saved, "w") as f:
            f.write("2\n")
        r._track_saves("j", j, True, 200.0)
        self.assertEqual(r.costs.get("j", "save"), (1500.0, True))
        j["state"], j["run_s"] = HOT, 1600.0
        self.assertEqual(r.unsaved_s("j"), 100.0)
        self.assertEqual(r.snapshot().jobs[0].save_s, 1500.0)

    def test_stepping_aside_reports_the_lost_work(self):
        r = self.build()
        r.refresh()
        self.assertTrue(r._resume_job("j", reason="test"))
        r.refresh()
        j = r.job["j"]
        j["run_s"], j["save_run_s"] = 500.0, 380.0
        self.assertTrue(r._park_job("j", reason="test"))
        lost = [e for e in r.recent_events() if e.get("event") == "lost_work"]
        self.assertEqual(lost[0]["seconds"], 120.0)
        self.assertEqual(j["save_run_s"], j["run_s"])                          # it starts again from its saved point


if __name__ == "__main__":
    unittest.main()
