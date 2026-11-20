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
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "park_s": 0.1, "hold": True,
                     "start_cmd": "sleep 60", "running": {"hbm_gib": 10, "cpu_gib": 50}}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        # the job is held until the engine is parked; the park takes 100 GiB (140 free before, 40 after) and a stop gives it back; 40 - 50 needed < 16: short
        r.host_mem = lambda: 40.0 if prod.sleeping and not os.path.exists(stopped) else 140.0
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        self.assertTrue(prod.sleeping, "the window parked the engine")
        r._set_hold("j", False)
        self.assertTrue(wait_for(lambda: any(e["event"] == "window_park_released" for e in r.recent)))
        rel = [e for e in r.recent if e["event"] == "window_park_released"][-1]
        self.assertEqual(rel["job"], "j")
        time.sleep(0.3)
        with open(stopped) as f:
            self.assertEqual(f.read().count("x"), 1, "stopped once, not every tick")
        os.remove(r.top["window_file"])
        self.assertTrue(wait_for(lambda: os.path.exists(started)), "the window's end starts it cold")
        self.assertFalse(any(e["event"] == "window_resume" for e in r.recent))

    def test_a_park_that_would_not_fit_in_host_memory_is_a_stop(self):
        # parking copies 100 GiB into host memory; with 20 GiB available that would run the host out of memory
        d = tempfile.mkdtemp()
        stopped, started = os.path.join(d, "stop-ran"), os.path.join(d, "start-ran")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True, "parked": {"cpu_gib": 100},
                         "stop_cmd": f"touch {stopped}", "start_cmd": f"touch {started}"}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        r.host_mem = lambda: 20.0
        self.assertEqual(chat(port)[0], 200)
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        self.assertTrue(os.path.exists(stopped), "stopped instead of parked")
        self.assertFalse(prod.sleeping, "never parked")
        self.assertFalse(r.svc["prod"].get("window_parked"))
        self.assertTrue(any(e["event"] == "window_park_skipped" for e in r.recent))
        os.remove(r.top["window_file"])
        self.assertTrue(wait_for(lambda: os.path.exists(started)), "the window's end starts it")
        self.assertFalse(any(e["event"] == "window_resume" for e in r.recent))

    def test_a_window_that_opens_during_the_last_one_s_resume_waits_for_it(self):
        # a resume runs outside the lock; a window opening meanwhile used to park the engine under it, and the resume
        # then left it awake while GapRunner counted it stopped and parked
        d = tempfile.mkdtemp()
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True, "stop_cmd": "true", "start_cmd": "true"}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        inner, began, go, parks = r.svc["prod"]["driver"], threading.Event(), threading.Event(), []
        self.addCleanup(go.set)

        class SlowResume:
            def __getattr__(self, k):
                return getattr(inner, k)

            def park(self):
                parks.append(began.is_set() and not go.is_set())    # True: parked while a resume was running
                inner.park()

            def resume(self):
                began.set()
                go.wait(10)
                inner.resume()
        r.svc["prod"]["driver"] = SlowResume()
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped" and prod.sleeping))
        os.remove(r.top["window_file"])
        self.assertTrue(began.wait(5), "the window's end resumes it")
        open_window(r)                                       # the next window opens while that resume is running
        time.sleep(0.5)
        self.assertEqual(parks, [False], "not parked under the running resume")
        go.set()
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped" and len(parks) == 2))
        self.assertTrue(prod.sleeping, "parked for the new window once the resume finished")
        self.assertTrue(r.svc["prod"]["window_parked"])
        self.assertFalse(r.svc["prod"]["window_resuming"])

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


class ReleaseTest(unittest.TestCase):
    """_release_if_short during a window, called directly on engines marked window-parked: which engines it stops."""

    def runner(self, parked=(100, 100), job=None, floor=16):
        d = tempfile.mkdtemp()
        self.log = os.path.join(d, "stopped")
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "cpu_floor_gib": floor},
            "service": [{"name": f"e{i}", "driver": "vllm", "upstream": "http://127.0.0.1:1", "listen": "127.0.0.1:0",
                         "gpu_gib": 10, "window_park": True, "parked": {"cpu_gib": gib},
                         "stop_cmd": f"echo e{i} >> {self.log}", "start_cmd": "true"} for i, gib in enumerate(parked)],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 10, "coexist": "share", "start_cmd": "true",
                     "running": {"hbm_gib": 10, "cpu_gib": 50}, **(job or {})}],
        })
        r = Runner(cfg, gpu=FakeGpu(100.0))
        for s in r.svc.values():
            s["stopped"], s["state"], s["window_parked"] = True, "stopped", True
        r.job["j"]["state"] = "parked"
        # 40 GiB free with every engine parked; each stop gives back what that engine holds parked
        r.host_mem = lambda: 40.0 + sum(parked[int(n[1:])] for n in self.stopped())
        open_window(r)
        return r

    def stopped(self):
        try:
            with open(self.log) as f:
                return f.read().split()
        except FileNotFoundError:
            return []

    def released(self, r):
        r._release_if_short(r.snapshot())
        return self.stopped()

    def test_engines_are_stopped_only_until_the_job_fits(self):
        r = self.runner(parked=(100, 100))
        self.assertEqual(self.released(r), ["e0"], "140 GiB free after one stop: 140 - 50 >= 16")
        self.assertTrue(r.svc["e1"]["window_parked"])
        r = self.runner(parked=(10, 20))
        self.assertEqual(self.released(r), ["e1", "e0"], "the largest first; both needed (40 + 20 + 10 - 50 >= 16)")

    def test_a_job_that_could_not_start_anyway_stops_nothing(self):
        for why, job in (("disabled", {"enabled": False}), ("held", {"hold": True}),
                         ("does not fit the GPU", {"gpu_gib": 200, "running": {"hbm_gib": 200, "cpu_gib": 50}}),
                         ("fits already", {"running": {"hbm_gib": 10, "cpu_gib": 10}})):
            with self.subTest(why=why):
                self.assertEqual(self.released(self.runner(job=job)), [])

    def test_measured_parked_host_memory_counts_when_none_is_declared(self):
        r = self.runner(parked=(0,))
        self.assertEqual(self.released(r), [], "declares 0 and nothing measured: stopping it frees nothing")
        r = self.runner(parked=(0,))
        for _ in range(3):
            r.costs.record(r.svc["e0"]["cost_key"], "park_host", 100.0)
        r.host_mem = lambda: 40.0 + (100.0 if self.stopped() else 0.0)
        self.assertEqual(self.released(r), ["e0"], "it was measured holding 100 GiB parked")

    def test_nothing_is_stopped_once_the_window_has_ended(self):
        r = self.runner()
        snap = r.snapshot()
        os.remove(r.top["window_file"])
        r._release_if_short(snap)
        self.assertEqual(self.stopped(), [])


if __name__ == "__main__":
    unittest.main()
