"""progress_every_s: a job's progress_cmd is read at the interval it asks for, 30 s when it asks for none."""
import os
import sys
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner.runner import Runner

PY = sys.executable


def runner(d, prog, **extra):
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", 20.0)
    cfg = C.parse({
        "gaprunner": {"mode": "live", "state_dir": d},
        "policy": {"reserve_gib": 0},
        "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20}],
        "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "park_s": 0.5,
                 "start_cmd": f"{PY} -c 'import time; time.sleep(60)'", "progress_cmd": f"cat {prog}", **extra}],
    })
    return Runner(cfg, gpu=gpu)


class ProgressEveryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.prog = os.path.join(self.d, "progress")
        open(self.prog, "w").write("3/8 passes · engine\ngenerating\n")

    def read_twice(self, r):
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        j = r.job["j"]
        r.tick()
        while j["state"] != "hot" or j["progress"] is None:   # started, then read once
            r.tick()
            time.sleep(0.05)
        self.assertEqual(j["progress"], 3 / 8)
        self.assertEqual(j["part_text"], "generating")
        open(self.prog, "w").write("6/8 passes · engine\ndecoding the video\n")
        time.sleep(1.2)
        r.tick()
        return j

    def test_short_interval_reads_again_within_seconds(self):
        j = self.read_twice(runner(self.d, self.prog, progress_every_s=1))
        self.assertEqual(j["progress"], 6 / 8)
        self.assertEqual(j["part_text"], "decoding the video")

    def test_default_interval_is_still_30_s(self):
        j = self.read_twice(runner(self.d, self.prog))
        self.assertEqual(j["progress"], 3 / 8)

    def test_interval_has_a_floor_of_one_second(self):
        r = runner(self.d, self.prog, progress_every_s=0)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        j = r.job["j"]
        while j["state"] != "hot" or j["progress"] is None:
            r.tick()
            time.sleep(0.05)
        open(self.prog, "w").write("6/8\n")
        r.tick()                                          # within the floor: not read again yet
        self.assertEqual(j["progress"], 3 / 8)


if __name__ == "__main__":
    unittest.main()
