"""A running job whose progress stops moving is flagged (report-only), and the flag clears once it moves again."""
import os
import sys
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import health
from gaprunner.runner import Runner

PY = sys.executable


class HealthTest(unittest.TestCase):
    def test_stall_raised_notified_and_cleared(self):
        health.CHECK_EVERY_S = 0
        d = tempfile.mkdtemp()
        prog, note = os.path.join(d, "progress"), os.path.join(d, "notes")
        open(prog, "w").write("3/10\n")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "notify_cmd": f'echo "$GAPRUNNER_LEVEL $GAPRUNNER_MSG" >> {note}'},
            "policy": {"reserve_gib": 0},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20}],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "stall_after_s": 0.5, "park_s": 0.5,
                     "start_cmd": f"{PY} -c 'import time; time.sleep(60)'", "progress_cmd": f"cat {prog}"}],
        })
        r = Runner(cfg, gpu=gpu)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        j = r.job["j"]

        def ticks(n, gap=0.1):
            for _ in range(n):
                j["progress_at"] = 0                       # read progress every tick in this test
                r.tick()
                time.sleep(gap)
        ticks(15)
        self.assertEqual(j["state"], "hot")
        self.assertTrue(any("no progress" in i for i in j.get("health", [])), j.get("health"))
        self.assertEqual(r.status()["jobs"]["j"]["word"], "stalled")
        open(prog, "w").write("4/10\n")                    # it moves again
        j["spec"]["stall_after_s"] = 3600                    # (and the test stops being impatient)
        ticks(6)
        self.assertEqual(j.get("health"), [])
        time.sleep(0.3)
        lines = open(note).read()
        self.assertIn("warn j: no progress", lines)
        self.assertIn("info j: back to normal", lines)


class HealthGraceTest(unittest.TestCase):
    def test_health_cmd_waits_for_startup_and_paused_time_is_not_clean(self):
        health.CHECK_EVERY_S = 0
        d = tempfile.mkdtemp()
        ok = os.path.join(d, "ok")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 20.0)
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": d}, "policy": {"reserve_gib": 0},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20}],
                       "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "yield", "park_s": 0.5,
                                "start_cmd": f"{PY} -c 'import time; time.sleep(60)'", "health_cmd": f"test -f {ok}"}]})
        r = Runner(cfg, gpu=gpu)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        j = r.job["j"]
        def ticks(n):
            for _ in range(n):
                j["health_cmd_at"] = 0
                r.tick()
                time.sleep(0.05)
        ticks(8)
        self.assertEqual(j["state"], "hot")
        self.assertEqual(j.get("health") or [], [])                # still starting: not flagged
        open(ok, "w").close()
        ticks(2)                                                    # first success
        os.remove(ok)
        ticks(2)
        self.assertTrue(j.get("health"))                            # failing after it was healthy: flagged
        r.request_start("prod")                                     # paused for prod
        ticks(6)
        self.assertTrue(j.get("health"))                            # paused rounds don't clear it
        r.request_end("prod")


if __name__ == "__main__":
    unittest.main()


class QuietLogTest(unittest.TestCase):
    """A job that logs elsewhere isn't stuck while its progress moves; it is once both stop."""

    def check(self, part_changed_run_s):
        import tempfile, types
        from gaprunner import health
        d = tempfile.mkdtemp()
        log = os.path.join(d, "job.log")
        open(log, "w").close()
        os.utime(log, (0, 0))                                  # written long ago
        runner = types.SimpleNamespace(top={"state_dir": d})
        j = {"spec": {"log_file": log, "stall_after_s": 600, "progress_cmd": "x", "running": {"hbm_gib": 10}}, "state": "hot", "paused": False,
             "frozen": False, "run_s": 5000.0, "hot_run_s": 0.0, "active_since": 0.0, "progress": 0.07,
             "progress_changed_run_s": 1000.0, "part_changed_run_s": part_changed_run_s}
        return health._issues_for_job(runner, "job", j, 10_000.0, {})

    def test_moving_second_line_is_not_stuck(self):
        self.assertEqual(self.check(4900.0), [])

    def test_nothing_moving_is_stuck(self):
        issues = self.check(1000.0)
        self.assertTrue(any("no progress" in x for x in issues), issues)
        self.assertTrue(any("log quiet" in x for x in issues), issues)
