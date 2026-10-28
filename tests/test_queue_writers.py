"""Changes to the queue file from the page and from GapRunner itself at the same moment: neither is lost."""
import tempfile
import threading
import unittest

from fakes import FakeGpu
from gaprunner import config as C
from gaprunner import ui
from gaprunner.runner import Runner


class QueueWritersTest(unittest.TestCase):
    def test_concurrent_queue_changes_are_all_kept(self):
        gpu = FakeGpu(100.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "driver": "vllm", "upstream": "http://127.0.0.1:9", "listen": "127.0.0.1:0",
                         "gpu_gib": 60}],
            "job": [{"name": "bench", "driver": "fake", "gpu_gib": 10, "gpu": gpu}],
        })
        r = Runner(cfg, gpu=gpu)
        n = 200

        def page():                              # someone adding ideas on the page
            for i in range(n):
                ui.act(r, {"action": "add", "name": f"idea {i}"})

        def runner():                            # GapRunner holding and releasing a job (Stop, max_hours, a park)
            for i in range(n):
                r._set_hold("bench", i % 2 == 0)
        ts = [threading.Thread(target=page), threading.Thread(target=runner)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        rows = {x["name"]: x for x in C.read_queue(r.top)["jobs"]}
        lost = [i for i in range(n) if f"idea {i}" not in rows]
        self.assertEqual(lost, [])
        self.assertFalse(rows["bench"]["hold"])   # the last change: i = 199


if __name__ == "__main__":
    unittest.main()
