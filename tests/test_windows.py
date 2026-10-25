"""Exclusive windows: stop the services, give a slow job the whole GPU, then bring the services back."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

from fakes import FakeGpu, FakeVllm
from test_live import chat
from gaprunner import config as C
from gaprunner import gate
from gaprunner.gaps import GapModel
from gaprunner.runner import Runner

PY = sys.executable


class ExclusiveWindowTest(unittest.TestCase):
    def test_stop_prod_run_big_job_then_restart(self):
        d = tempfile.mkdtemp()
        down = os.path.join(d, "prod-down")
        class Gpu(FakeGpu):                       # like the driver: a stopped container's memory is free at once
            def read(self, fresh=False):
                if os.path.exists(down):
                    self.resident.pop("prod", None)
                return super().read(fresh)
        gpu = Gpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0, down_file=down)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "stop_cmd": f"touch {down}", "start_cmd": f"rm -f {down}"}],
            "job": [{"name": "big", "driver": "restart", "gpu_gib": 90, "window_only": True, "timeout_s": 2,
                     "start_cmd": f"{PY} -c 'import time; time.sleep(60)'"}],
        })
        r = Runner(cfg, gpu=gpu)
        r.svc["prod"]["gaps"] = GapModel([600] * 50, min_samples=20)
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(r, "prod"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        stop = threading.Event()
        self.addCleanup(stop.set)
        self.addCleanup(lambda: r.job["big"]["driver"].kill())

        def loop():
            while not stop.is_set():
                r.tick()
                time.sleep(0.05)
        threading.Thread(target=loop, daemon=True).start()

        def wait_for(cond, timeout=8):
            end = time.time() + timeout
            while time.time() < end:
                if cond():
                    return True
                time.sleep(0.05)
            return False

        self.assertEqual(chat(port)[0], 200)
        with open(r.top["window_file"], "w") as f:
            json.dump({"until": time.time() + 3600, "exclusive": True}, f)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        self.assertTrue(wait_for(lambda: r.job["big"]["state"] == "hot"))        # the 90 GiB job gets the GPU
        code, body = chat(port)
        self.assertEqual(code, 503)
        self.assertIn(b"is off (no prod)", body)
        os.remove(r.top["window_file"])                                           # close the window early
        self.assertTrue(wait_for(lambda: r.job["big"]["state"] == "parked"))
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "hot"))
        self.assertEqual(chat(port)[0], 200)
        ev = [e["event"] for e in r.recent]
        self.assertIn("service_stop", ev)
        self.assertIn("service_ready", ev)


if __name__ == "__main__":
    unittest.main()
