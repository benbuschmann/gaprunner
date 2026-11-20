"""A governor job's live request: the host memory its app says the work adds (estimated_incremental_gib).

Tested with the real admission client message and the real GovernorJob: no estimate, or a zero or negative one, means
the job's configured worst case applies; a usable estimate is what the scheduler and the window-park release check.
"""
import importlib.util
import json
import os
import socket
import tempfile
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
        for bad in ("lots", None, True, float("nan")):
            with self.subTest(bad=bad):
                j = self.job()
                r, app = ask(j, {"op": "reserve", "owner": "studio", "estimated_incremental_gib": bad})
                self.addCleanup(app.close)
                self.assertIsNone(j.asked_cpu_gib())
                j.gone(r)


class LiveRequestWindowParkTest(unittest.TestCase):
    def start(self, d, stopped, use=True):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0, "park_only_if_work": False, "cpu_floor_gib": 16},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "idle_park_s": 600, "window_park": True, "parked": {"cpu_gib": 100},
                         "stop_cmd": f"echo x >> {stopped}", "start_cmd": "true"}],
            "job": [{"name": "v", "driver": "governor", "owner": "studio", "gpu_gib": 10, "coexist": "share",
                     "grace_s": 1, "running": {"hbm_gib": 10, "cpu_gib": 50}, "use_request_estimate": use}],
        })
        r, port, stop = run(cfg, gpu)
        self.addCleanup(stop.set)
        r.governor.install()                         # the folder the app reads (no socket: requests are put in directly)
        r.host_mem = lambda: 40.0                    # configured 50: short; asked 20: 40 - 20 >= 16
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


if __name__ == "__main__":
    unittest.main()
