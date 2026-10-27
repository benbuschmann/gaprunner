"""A service started at a window's end must not be idle-parked before GapRunner has seen it ready.

Seen live on a GB300 (0.24.1, twice in one day): prod came up idle after a window, the tick's
refresh() called it HOT, decide() parked it for being idle, and _windows() then never saw it HOT again, so
starting_since stayed set and the gate answered 503 "starting again" until GapRunner was restarted.
"""
import json
import os
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
class WindowEndStartIdleTest(unittest.TestCase):
    def test_idle_service_started_at_window_end_becomes_ready(self):
        d = tempfile.mkdtemp()
        down = os.path.join(d, "prod-down")
        class Gpu(FakeGpu):
            def read(self, fresh=False):
                if os.path.exists(down):
                    self.resident.pop("prod", None)
                return super().read(fresh)
        gpu = Gpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0, down_file=down)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 0.2, "stop_cmd": f"touch {down}", "start_cmd": f"(sleep 0.5; rm -f {down}) >/dev/null 2>&1 &"}],   # boots after the start returns
        })
        r = Runner(cfg, gpu=gpu)
        r.svc["prod"]["gaps"] = GapModel([600] * 50, min_samples=20)
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(r, "prod"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        stop = threading.Event()
        self.addCleanup(stop.set)
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
        with open(r.top["window_file"], "w") as f:
            json.dump({"until": time.time() + 3600, "exclusive": True}, f)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        time.sleep(0.5)                                   # prod's last request is now older than idle_park_s
        os.remove(r.top["window_file"])
        self.assertTrue(wait_for(lambda: any(e["event"] == "service_ready" for e in r.recent)))
        self.assertIsNone(r.svc["prod"]["starting_since"])
        code, body = chat(port)                           # a parked prod wakes for the request; no 503
        self.assertEqual(code, 200, body)
if __name__ == "__main__":
    unittest.main()
