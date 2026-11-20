"""A governor job's live request: the host memory its app says the work adds (estimated_incremental_gib).

Tested with the real admission client message and the real GovernorJob: no estimate, or a zero or negative one, means
the job's configured worst case applies; a usable estimate is what the scheduler and the window-park release check.
"""
import importlib.util
import json
import os
import socket
import tempfile
import time
import unittest

from fakes import FakeGpu, FakeVllm
from test_window_park import open_window, run, wait_for
from gaprunner import config as C
from gaprunner import governor as GV

_spec = importlib.util.spec_from_file_location(
    "admission_client_under_test", os.path.join(os.path.dirname(GV.__file__), "governor_client", "admission_client.py"))
AC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AC)

PEER = {"pid": None, "uid": os.getuid()}


def client_message(*flags):
    """The reserve message the admission client sends for these flags, as GapRunner reads it off the socket."""
    args, _ = AC.parse(["--owner", "studio", "--socket", "/nonexistent", *flags, "--run", "true"])
    return json.loads(json.dumps(AC.reserve_request(args)))


def ask(job, msg):
    """Put an app's request in the GovernorJob's queue as the socket server does; returns (request, app's end)."""
    mine, app = socket.socketpair()
    r, why = job.ask(msg, mine, PEER)
    assert r is not None, why
    return r, app


class EstimateTest(unittest.TestCase):
    def job(self):
        return GV.GovernorJob({"name": "v", "driver": "governor", "owner": "studio", "timeout_s": 5, "grace_s": 1})

    def test_no_estimate_zero_or_negative_means_the_configured_worst_case(self):
        msg = client_message()
        self.assertNotIn("estimated_incremental_gib", msg, "the client leaves out an estimate it wasn't given")
        for flags, want in (((), None), (("--estimated-incremental-gib", "0"), None),
                            (("--estimated-incremental-gib", "-5"), None), (("--estimated-incremental-gib", "20"), 20.0)):
            with self.subTest(flags=flags):
                j = self.job()
                r, app = ask(j, client_message(*flags))
                self.addCleanup(app.close)
                self.assertEqual(j.asked_cpu_gib(), want)
                j.gone(r)
        for bad in ("lots", None, True, float("nan"), 10 ** 400):
            with self.subTest(bad=bad):
                j = self.job()
                r, app = ask(j, {"op": "reserve", "owner": "studio", "estimated_incremental_gib": bad})
                self.addCleanup(app.close)
                self.assertIsNone(j.asked_cpu_gib())
                j.gone(r)


class LiveRequestWindowParkTest(unittest.TestCase):
    def start(self, d, stopped, use=True, **job):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True, "parked": {"cpu_gib": 100},
                         "stop_cmd": f"echo x >> {stopped}", "start_cmd": "true"}],
            "job": [{"name": "v", "driver": "governor", "owner": "studio", "gpu_gib": 10, "coexist": "share",
                     "grace_s": 1, "running": {"hbm_gib": 10, "cpu_gib": 50}, "use_request_estimate": use, **job}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        r.governor.install()                         # the folder the app reads (no socket: requests are put in directly)
        # the park takes 100 GiB (140 free before, 40 after) and a stop gives it back; with the engine parked a job
        # checked at its configured 50 GiB is short, one asking 20 GiB fits (40 - 20 >= 16)
        r.host_mem = lambda: 40.0 if prod.sleeping and not os.path.exists(stopped) else 140.0
        return r, prod

    def test_a_request_whose_estimate_fits_starts_beside_the_window_parked_engine(self):
        # Seen on a DGX Station: Imagination Studio asked the governor for 95 GiB with 140 GiB free, but was checked at
        # its configured 165 GiB, so the parked engine was stopped and cost a 385 s cold start. The live request decides.
        d = tempfile.mkdtemp()
        stopped = os.path.join(d, "stop-ran")
        r, prod = self.start(d, stopped)
        gj = r.job["v"]["driver"]
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        req, app = ask(gj, client_message("--estimated-incremental-gib", "20"))
        self.addCleanup(app.close)
        self.assertTrue(wait_for(lambda: req.granted.is_set()), "the request is admitted on its estimate")
        self.assertTrue(json.loads(app.makefile("r").readline())["allowed"])
        self.assertTrue(prod.sleeping, "the engine stays parked")
        self.assertFalse(os.path.exists(stopped))
        self.assertFalse(any(e["event"] == "window_park_released" for e in r.recent))

    def test_a_request_without_an_estimate_is_checked_at_the_configured_worst_case(self):
        d = tempfile.mkdtemp()
        stopped = os.path.join(d, "stop-ran")
        r, prod = self.start(d, stopped)
        gj = r.job["v"]["driver"]
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        req, app = ask(gj, client_message())
        self.addCleanup(app.close)
        self.assertTrue(wait_for(lambda: any(e["event"] == "window_park_released" for e in r.recent)),
                        "50 GiB configured with 40 free: the parked engine gives way")
        self.assertTrue(os.path.exists(stopped))

    def test_a_window_s_park_and_a_grant_in_the_same_tick_do_not_share_the_same_memory(self):
        # The request waits for the window (window_only). When the window opens, admitting it (50 GiB) and parking the
        # engine (100 GiB) each fit the 140 GiB free alone, not together; a grant shows in free memory only once the
        # app has allocated, so the park must come first and the job be planned on what is left.
        d = tempfile.mkdtemp()
        stopped = os.path.join(d, "stop-ran")
        r, prod = self.start(d, stopped, use=False, window_only=True)
        gj = r.job["v"]["driver"]
        req, app = ask(gj, client_message())
        self.addCleanup(app.close)
        time.sleep(0.3)
        self.assertFalse(req.granted.is_set(), "waits for the window")
        open_window(r)
        self.assertTrue(wait_for(lambda: req.granted.is_set()))
        self.assertFalse(prod.sleeping and not os.path.exists(stopped),
                         "not admitted beside an engine whose park took the memory it was planned on")
        self.assertTrue(any(e["event"] == "window_park_released" for e in r.recent))

    def test_the_estimate_is_used_only_when_the_job_opts_in(self):
        d = tempfile.mkdtemp()
        stopped = os.path.join(d, "stop-ran")
        r, prod = self.start(d, stopped, use=False)
        open_window(r)
        self.assertTrue(wait_for(lambda: r.svc["prod"]["state"] == "stopped"))
        req, app = ask(r.job["v"]["driver"], client_message("--estimated-incremental-gib", "20"))
        self.addCleanup(app.close)
        self.assertTrue(wait_for(lambda: any(e["event"] == "window_park_released" for e in r.recent)),
                        "without use_request_estimate the configured 50 GiB is checked")
        self.assertEqual([j.run_cpu_gib for j in r.snapshot().jobs], [50.0])

    def test_use_request_estimate_is_a_boolean_on_governor_jobs(self):
        base = {"gaprunner": {"state_dir": tempfile.mkdtemp()},
                "service": [{"name": "prod", "driver": "vllm", "upstream": "http://127.0.0.1:1",
                             "listen": "127.0.0.1:0", "gpu_gib": 10}]}
        gov = {"name": "v", "driver": "governor", "owner": "studio", "gpu_gib": 10}
        self.assertFalse(C.parse({**base, "job": [gov]})["jobs"][0]["use_request_estimate"])
        with self.assertRaisesRegex(C.ConfigError, "true or false"):
            C.parse({**base, "job": [{**gov, "use_request_estimate": "yes"}]})
        with self.assertRaisesRegex(C.ConfigError, "governor"):
            C.parse({**base, "job": [{"name": "t", "driver": "restart", "start_cmd": "true", "gpu_gib": 1,
                                      "use_request_estimate": True}]})


class RequestIdentityTest(unittest.TestCase):
    """The estimate the scheduler checked belongs to one request: if that request leaves and another takes its place
    before the grant, the newcomer is not admitted on the old figure."""

    def test_a_request_that_replaced_the_planned_one_is_planned_again(self):
        from gaprunner.runner import Runner
        d = tempfile.mkdtemp()
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        self.addCleanup(prod.srv.shutdown)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d},
            "policy": {"reserve_gib": 0, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60}],
            "job": [{"name": "v", "driver": "governor", "owner": "studio", "gpu_gib": 10, "grace_s": 1,
                     "running": {"hbm_gib": 10, "cpu_gib": 50}, "use_request_estimate": True}],
        })
        r = Runner(cfg, gpu=gpu)
        r.governor.install()
        r.host_mem = lambda: 100.0
        gj = r.job["v"]["driver"]
        a, app_a = ask(gj, client_message("--estimated-incremental-gib", "20"))
        self.addCleanup(app_a.close)
        planned = r.snapshot().jobs[0]
        self.assertEqual((planned.run_cpu_gib, planned.ask_id), (20.0, a.token))
        gj.gone(a)                                                   # A leaves; B, much larger, asks at once
        b, app_b = ask(gj, client_message("--estimated-incremental-gib", "200"))
        self.addCleanup(app_b.close)
        self.assertFalse(r._resume_job("v", "fits", ask_id=planned.ask_id))
        self.assertFalse(b.granted.is_set(), "B is not admitted on A's 20 GiB")
        self.assertTrue(any(e["event"] == "resume_skipped" and e["target"] == "v" for e in r.recent))
        again = r.snapshot().jobs[0]
        self.assertEqual((again.run_cpu_gib, again.ask_id), (200.0, b.token), "the next plan uses B's own estimate")
        # the same check inside the driver, for a request that changes between that check and the grant
        with self.assertRaises(GV.RequestChanged):
            gj.resume(request_id=a.token)
        self.assertFalse(b.granted.is_set())
        gj.resume(request_id=b.token)
        self.assertTrue(b.granted.is_set())

    def runner(self):
        from gaprunner.runner import Runner
        d = tempfile.mkdtemp()
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        self.addCleanup(prod.srv.server_close)
        self.addCleanup(prod.srv.shutdown)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d},
            "policy": {"reserve_gib": 0, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60}],
            "job": [{"name": "v", "driver": "governor", "owner": "studio", "gpu_gib": 10, "grace_s": 1,
                     "running": {"hbm_gib": 10, "cpu_gib": 50}, "use_request_estimate": True}],
        })
        r = Runner(cfg, gpu=gpu)
        r.governor.install()
        r.host_mem = lambda: 140.0
        return r

    def test_a_request_planned_at_the_configured_figure_keeps_its_identity(self):
        # A has no estimate, so it is planned at the configured 50 GiB; B, asking 200 GiB, replaces it before the grant
        r = self.runner()
        gj = r.job["v"]["driver"]
        a, app_a = ask(gj, client_message())
        self.addCleanup(app_a.close)
        planned = r.snapshot().jobs[0]
        self.assertEqual((planned.run_cpu_gib, planned.ask_id), (50.0, a.token))
        gj.gone(a)
        b, app_b = ask(gj, client_message("--estimated-incremental-gib", "200"))
        self.addCleanup(app_b.close)
        self.assertFalse(r._resume_job("v", "fits", ask_id=planned.ask_id))
        self.assertFalse(b.granted.is_set(), "B is not admitted on A's plan")

    def test_a_plan_made_with_no_request_grants_nothing(self):
        r = self.runner()
        gj = r.job["v"]["driver"]
        planned = r.snapshot().jobs[0]
        self.assertEqual(planned.ask_id, "")
        b, app_b = ask(gj, client_message("--estimated-incremental-gib", "200"))
        self.addCleanup(app_b.close)
        self.assertFalse(r._resume_job("v", "fits", ask_id=planned.ask_id))
        self.assertFalse(b.granted.is_set())


if __name__ == "__main__":
    unittest.main()
