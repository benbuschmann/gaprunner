"""A service isn't put back to sleep while its warm_cmd runs, and one that fails to wake fails requests fast and is
restarted (stop_cmd, start_cmd) instead of every request waiting out another wake."""
import os
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from test_scheduler import LONG, POL, job, kinds, snap
from gaprunner import config as C
from gaprunner.runner import Runner
from gaprunner.scheduler import PARKED, STARTING, decide


class WarmingTest(unittest.TestCase):
    def test_a_warming_service_stays_awake(self):
        s = snap(jobs=[job("a", order=0)])
        self.assertIn(("park_service", "prod"), kinds(decide(s, LONG, POL)))
        s.services[0].warming = True
        self.assertNotIn(("park_service", "prod"), kinds(decide(s, LONG, POL)))

    def test_the_hold_ends_after_warm_hold_max_s(self):
        r, _ = build()
        r.svc["prod"]["warm_since"] = r.clock() - 10
        self.assertTrue(r.snapshot().services[0].warming)
        r.svc["prod"]["warm_since"] = r.clock() - 400
        self.assertFalse(r.snapshot().services[0].warming)


def build(restart=True):
    d = tempfile.mkdtemp()
    log = os.path.join(d, "cmds")
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", 20.0)
    svc = {"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20,
           "restart_on_failed_wake": restart}
    if restart:
        svc.update(stop_cmd=f"echo stop >> {log}", start_cmd=f"echo start >> {log}")
    cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": d}, "service": [svc]})
    r = Runner(cfg, gpu=gpu)
    r.refresh()
    return r, log


class FailedWakeTest(unittest.TestCase):
    def fail_wake(self, r):
        s = r.svc["prod"]
        s["state"] = PARKED

        def boom():
            raise RuntimeError("timed out")
        s["driver"].resume = boom
        self.assertFalse(r.ensure_hot("prod"))

    def test_requests_fail_fast_and_the_service_is_restarted(self):
        r, log = build()
        self.fail_wake(r)
        self.assertIn("failed to wake", r.unavailable("prod"))
        t0 = time.monotonic()
        self.assertFalse(r.ensure_hot("prod"))                  # no second wake attempt
        self.assertLess(time.monotonic() - t0, 0.5)
        r._recover()
        self.assertEqual(open(log).read().split(), ["stop", "start"])
        self.assertEqual(r.svc["prod"]["state"], STARTING)
        self.assertIsNone(r.svc["prod"]["dead_since"])
        self.assertIn("starting again", r.unavailable("prod"))

    def test_without_restart_commands_a_wake_is_tried_again_later(self):
        r, log = build(restart=False)
        self.fail_wake(r)
        r._recover()
        self.assertIsNotNone(r.svc["prod"]["dead_since"])       # too soon to try again
        r.svc["prod"]["dead_since"] -= 61
        r._recover()
        self.assertIsNone(r.svc["prod"]["dead_since"])
        self.assertFalse(os.path.exists(log))


if __name__ == "__main__":
    unittest.main()
