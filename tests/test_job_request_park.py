"""A job's own request to a service (/_job/<name>/...) that arrives after the tick's snapshot keeps the service from
being parked under it, as an ordinary request does."""
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner.gaps import GapModel
from gaprunner.runner import Runner


class JobRequestParkTest(unittest.TestCase):
    def test_a_job_request_after_the_snapshot_keeps_the_service_hot(self):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp(), "verify_free_s": 0.5},
            "policy": {"p_min": 0.5, "worth_factor": 1, "juggle_s": 0, "reserve_gib": 0},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 0.1, "wake_s": 0.05}],
            "job": [{"name": "bench", "driver": "fake", "gpu_gib": 70, "park_s": 0.1, "resume_s": 0.1, "gpu": gpu}],
        })
        r = Runner(cfg, gpu=gpu)
        r.svc["prod"]["gaps"] = GapModel([600] * 50, min_samples=20)
        r.svc["prod"]["last_done"] = time.time() - 60          # idle: the tick wants to park it for bench
        real = r.snapshot
        def snapshot():
            snap = real()
            r.request_start("prod", job="bench")                # bench's request lands right after the snapshot
            return snap
        r.snapshot = snapshot
        r.tick()
        self.assertEqual(r.svc["prod"]["state"], "hot")
        self.assertFalse(prod.sleeping)
        self.assertNotIn("park", [e["event"] for e in r.recent if e.get("target") == "prod"])


if __name__ == "__main__":
    unittest.main()
