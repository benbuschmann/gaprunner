"""State files with valid JSON of the wrong shape (a hand edit, another tool): GapRunner starts, ticks and wakes
services as if the file weren't there, instead of failing every tick or every start."""
import json
import os
import tempfile
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner.runner import Runner


def config(d, prod):
    return C.parse({
        "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
        "policy": {"reserve_gib": 0, "juggle_s": 0},
        "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                     "wake_s": 0.05}],
        "job": [{"name": "bench", "driver": "fake", "gpu_gib": 10, "coexist": "yield", "gpu": prod.gpu,
                 "start": "hot"}],
    })


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class BadStateFileTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.gpu = FakeGpu(100.0)
        self.prod = FakeVllm(self.gpu, "prod", 60.0)

    def test_a_window_file_of_the_wrong_shape_is_no_window(self):
        for bad in ({"until": "06:00"}, [], "tomorrow"):
            r = Runner(config(self.d, self.prod), gpu=self.gpu)
            write(r.top["window_file"], bad)
            self.assertIsNone(C.window_file_info(r.top), bad)
            r.tick()
            self.prod.sleeping = True                 # prod asleep: a request has to wake it
            r.svc["prod"]["state"] = "parked"
            self.assertTrue(r.ensure_hot("prod"), bad)
            r.tick()
            self.assertEqual([e["event"] for e in r.recent].count("window_file_ignored"), 1, bad)   # said once

    def test_a_queue_file_of_the_wrong_shape_is_an_empty_queue(self):
        for bad in ([], {"jobs": {}}, {"jobs": [1, "x", {"name": "bench", "hold": True}]}):
            r = Runner(config(self.d, self.prod), gpu=self.gpu)
            write(r.top["queue_file"], bad)
            r.tick()
            r.request_start("prod")
            r.request_end("prod")
            r.status()
            self.assertEqual(r.svc["prod"]["inflight"], 0)
        self.assertTrue(C.queue_overrides(r.top)["bench"]["hold"])     # the good row in a list of bad ones still counts

    def test_other_state_files_of_the_wrong_shape_dont_stop_the_start(self):
        for name in ("costs.json", "gaps-prod.json", "submitted.json"):
            for bad in ([], {"samples": 3, "gaps": 3, "jobs": 3}, {"samples": {"x": 1}, "gaps": ["a"], "jobs": [1]}):
                write(os.path.join(self.d, name), bad)
                Runner(config(self.d, self.prod), gpu=self.gpu)
                os.remove(os.path.join(self.d, name))


if __name__ == "__main__":
    unittest.main()
