"""window_park = true: an exclusive window parks the engine instead of stopping it, and resumes it when the window ends.

Engines that park in seconds but cold-start in minutes (a big MoE served by an engine with an in-place park) lose
the start time at every window otherwise. A park that fails falls back to stop_cmd, so the GPU is always given up.
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


def run(cfg, gpu):
    r = Runner(cfg, gpu=gpu)
    r.svc["prod"]["gaps"] = GapModel([600] * 50, min_samples=20)
    srv = gate.Server(("127.0.0.1", 0), gate.make_handler(r, "prod"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            r.tick()
            time.sleep(0.05)
    threading.Thread(target=loop, daemon=True).start()
    return r, srv.server_address[1], stop


def wait_for(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def open_window(r):
    with open(r.top["window_file"], "w") as f:
        json.dump({"until": time.time() + 3600, "exclusive": True}, f)


class WindowParkTest(unittest.TestCase):
    def test_window_parks_and_resumes_instead_of_stop_and_start(self):
        d = tempfile.mkdtemp()
        stopped, started = os.path.join(d, "stop-ran"), os.path.join(d, "start-ran")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True,
                         "stop_cmd": f"touch {stopped}", "start_cmd": f"touch {started}"}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        self.assertEqual(chat(port)[0], 200)
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        self.assertTrue(prod.sleeping, "the window parked the engine")
        self.assertFalse(os.path.exists(stopped), "stop_cmd did not run")
        self.assertTrue(any(e["event"] == "park" and e.get("reason") == "exclusive window" for e in r.recent))
        code, _ = chat(port)
        self.assertEqual(code, 503, "the window still keeps requests out")
        os.remove(r.top["window_file"])
        self.assertTrue(wait_for(lambda: any(e["event"] == "service_ready" for e in r.recent)))
        self.assertFalse(prod.sleeping)
        self.assertFalse(os.path.exists(started), "start_cmd did not run: the engine was resumed")
        self.assertTrue(any(e["event"] == "window_resume" for e in r.recent))
        self.assertEqual(chat(port)[0], 200)

    def test_a_failed_window_park_falls_back_to_stop_and_start(self):
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
            "service": [{"name": "prod", "driver": "command", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True,
                         "park_cmd": "exit 3", "resume_cmd": "true",
                         "state_cmd": f"test -f {down} && echo parked || echo hot",
                         "stop_cmd": f"touch {down}", "start_cmd": f"(sleep 0.3; rm -f {down}) >/dev/null 2>&1 &"}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        self.assertTrue(os.path.exists(down), "the failed park fell back to stop_cmd")
        self.assertTrue(any(e["event"] == "park_failed" for e in r.recent))
        os.remove(r.top["window_file"])
        self.assertTrue(wait_for(lambda: any(e["event"] == "service_ready" for e in r.recent)))
        self.assertTrue(any(e["event"] == "service_start" for e in r.recent), "and start_cmd brought it back")
        self.assertEqual(chat(port)[0], 200)

    def test_config_needs_a_stop_to_fall_back_to_and_command_hooks(self):
        base = {"gaprunner": {"state_dir": tempfile.mkdtemp()},
                "service": [{"name": "prod", "driver": "command", "upstream": "http://127.0.0.1:1", "listen": "127.0.0.1:0",
                             "gpu_gib": 10, "window_park": True}]}
        with self.assertRaisesRegex(C.ConfigError, "window_park needs stop_cmd"):
            C.parse(base)
        base["service"][0].update(stop_cmd="true", start_cmd="true")
        with self.assertRaisesRegex(C.ConfigError, "park_cmd and resume_cmd"):
            C.parse(base)
        base["service"][0].update(park_cmd="true", resume_cmd="true")
        C.parse(base)


if __name__ == "__main__":
    unittest.main()
