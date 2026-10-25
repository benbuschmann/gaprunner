"""Calibration runs a real process through start / warm / park / resume and writes measured recipe values."""
import sys
import tempfile
import time
import unittest

from fakes import FakeVllm
from gaprunner import calibrate
from gaprunner import config as C
from gaprunner import drivers
from gaprunner.runner import Runner

PY = sys.executable


class PidGpu:
    """A fake GPU on which every live process of the job holds 12 GiB."""
    def __init__(self, runner_ref):
        self.ref = runner_ref

    def read(self, fresh=False):
        r = self.ref[0]
        procs = []
        if r is not None:
            lead = drivers.leaders(r.job["j"]["spec"])
            procs = [{"pid": p, "gib": 12.0, "name": "python"} for p in lead]
        used = sum(p["gib"] for p in procs) + 20.0
        return {"name": "fake", "total_gib": 100.0, "used_gib": used, "util": None, "power_w": None, "temp_c": None,
                "procs": procs}


class CalibrateTest(unittest.TestCase):
    def test_measures_and_writes_a_recipe_block(self):
        prod = FakeVllm(None, "prod", 20.0)
        ref = [None]
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp(), "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20}],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 12, "hold": True, "coexist": "share", "timeout_s": 2,
                     "start_cmd": f"{PY} -c 'import time; time.sleep(60)'", "ssd_paths": [tempfile.gettempdir()]}],
        })
        r = Runner(cfg, gpu=PidGpu(ref))
        ref[0] = r
        r.refresh()
        m = calibrate.run(r, "j", warm_s=0.6, cycles=2, settle_s=0.3, poll_s=0.1)
        self.addCleanup(r.job["j"]["driver"].kill)
        self.assertEqual(m["status"], "ok", m["notes"])
        self.assertEqual(m["running"]["hbm_gib"], 12.0)
        self.assertEqual(m["parked"]["hbm_gib"], 0.0)
        self.assertIsNotNone(m["park_s"])
        self.assertIsNotNone(m["resume_s"])
        self.assertIn("hbm_gib = 12.0", m["toml"])
        self.assertTrue(r.costs.get("j", "park")[1])                  # measured swap times now drive the scheduler
        self.assertEqual(calibrate.load(r, "j")["status"], "ok")
        r.refresh()
        self.assertEqual(r.job["j"]["state"], "parked")               # left parked, not restarted, still held

    def test_refuses_when_it_does_not_fit(self):
        prod = FakeVllm(None, "prod", 20.0)
        ref = [None]
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20}],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 500, "start_cmd": "true"}],
        })
        r = Runner(cfg, gpu=PidGpu(ref))
        ref[0] = r
        r.refresh()
        m = calibrate.run(r, "j", warm_s=0.2, cycles=1)
        self.assertEqual(m["status"], "refused")
        self.assertIn("No prod", m["notes"][0])


if __name__ == "__main__":
    unittest.main()
