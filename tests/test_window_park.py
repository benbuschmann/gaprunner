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

    def test_a_job_short_of_host_memory_gets_the_window_parked_engine_stopped(self):
        # Seen on a DGX Station: the window-parked engine kept a host-memory copy of its GPU image and the window's job
        # waited 5 min for room that never came; the parked engine is stopped instead and starts cold when the window ends.
        d = tempfile.mkdtemp()
        stopped, started = os.path.join(d, "stop-ran"), os.path.join(d, "start-ran")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True, "parked": {"cpu_gib": 100},
                         "stop_cmd": f"echo x >> {stopped}", "start_cmd": f"touch {started}"}],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "park_s": 0.1,
                     "start_cmd": "sleep 60", "running": {"hbm_gib": 10, "cpu_gib": 50}}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        r.host_mem = lambda: 40.0                           # 40 free - 50 needed < 16: the job is short throughout
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        self.assertTrue(prod.sleeping, "the window parked the engine")
        self.assertTrue(wait_for(lambda: any(e["event"] == "window_park_released" for e in r.recent)))
        rel = [e for e in r.recent if e["event"] == "window_park_released"][-1]
        self.assertEqual(rel["job"], "j")
        time.sleep(0.3)
        with open(stopped) as f:
            self.assertEqual(f.read().count("x"), 1, "stopped once, not every tick")
        os.remove(r.top["window_file"])
        self.assertTrue(wait_for(lambda: os.path.exists(started)), "the window's end starts it cold")
        self.assertFalse(any(e["event"] == "window_resume" for e in r.recent))

    def test_a_job_whose_live_request_fits_starts_beside_the_window_parked_engine(self):
        # Seen on a DGX Station: Imagination Studio asked the governor for 95 GiB with 140 GiB free, but was checked at
        # its configured 165 GiB, so the parked engine was stopped and cost a 385 s cold start. The live request decides.
        d = tempfile.mkdtemp()
        stopped = os.path.join(d, "stop-ran")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True, "parked": {"cpu_gib": 100},
                         "stop_cmd": f"touch {stopped}", "start_cmd": "true"}],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "park_s": 0.1,
                     "start_cmd": "sleep 60", "running": {"hbm_gib": 10, "cpu_gib": 50}}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        r.host_mem = lambda: 40.0                           # configured 50: short; asked 20: 40 - 20 >= 16
        r.job["j"]["driver"].asked_cpu_gib = lambda: 20.0
        open_window(r)
        self.assertTrue(wait_for(lambda: r.job["j"]["state"] == "hot"), "the job starts on its live request")
        self.assertTrue(prod.sleeping, "the engine stays parked")
        self.assertFalse(os.path.exists(stopped))
        self.assertFalse(any(e["event"] == "window_park_released" for e in r.recent))

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
