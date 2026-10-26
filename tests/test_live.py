"""End to end: config -> Runner -> gate, with a fake vLLM prod and a fake job sharing a fake 100 GiB GPU."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest

from fakes import FakeGpu, FakeJob, FakeVllm
from gaprunner import config as C
from gaprunner import gate
from gaprunner.gaps import GapModel
from gaprunner.runner import Runner


def build(mode="live", stuck=False, job_start="parked", extra_jobs=(), job=None, policy=None, service=None):
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", 60.0)
    d = tempfile.mkdtemp()
    cfg = C.parse({
        "gaprunner": {"mode": mode, "state_dir": d, "tick_s": 0.05, "verify_free_s": 1.0},
        "policy": {"p_min": 0.5, "worth_factor": 1, "juggle_s": 0, "reserve_gib": 0, **(policy or {})},
        "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                     "idle_park_s": 0.3, "wake_s": 0.05, **(service or {})}],
        "job": [{"name": "bench", "driver": "fake", "gpu_gib": 70, "park_s": 0.1, "resume_s": 0.1, "gpu": gpu,
                 "stuck": stuck, "start": job_start, **(job or {})}, *[{"driver": "fake", "gpu_gib": 70, "park_s": 0.1, "resume_s": 0.1,
                                                          "gpu": gpu, "start": "parked", "hold": True, **x} for x in extra_jobs]],
    })
    r = Runner(cfg, gpu=gpu)
    r.svc["prod"]["gaps"] = GapModel([600] * 50, min_samples=20)      # history says gaps are long
    srv = gate.Server(("127.0.0.1", 0), gate.make_handler(r, "prod"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            r.tick()
            time.sleep(0.05)
    threading.Thread(target=loop, daemon=True).start()
    return r, prod, gpu, srv, stop


def chat(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/v1/chat/completions", body=b'{"stream":true}', headers={"Content-Type": "application/json"})
    resp = c.getresponse()
    body = resp.read()
    c.close()
    return resp.status, body


def events(r):
    return [json.loads(l) for l in open(r.audit_path)]


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.stops = []

    def tearDown(self):
        for s in self.stops:
            s.set()

    def mk(self, **kw):
        r, prod, gpu, srv, stop = build(**kw)
        self.stops.append(stop)
        return r, prod, gpu, srv.server_address[1]

    def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_idle_prod_parks_then_job_resumes_without_overlap(self):
        r, prod, gpu, port = self.mk()
        self.assertTrue(self.wait_for(lambda: FakeJob.registry["bench"].st == "hot"))
        self.assertTrue(prod.sleeping)
        self.assertEqual(set(gpu.resident), {"bench"})          # never both on the card (60 + 70 > 100)
        # the job reports hot a moment before the runner writes its "resume" event: wait for the event too
        self.assertTrue(self.wait_for(lambda: "resume" in [e["event"] for e in events(r)]))
        ev = [e["event"] for e in events(r)]
        self.assertLess(ev.index("park"), ev.index("resume"))

    def test_request_parks_job_then_wakes_prod_and_streams(self):
        r, prod, gpu, port = self.mk()
        self.wait_for(lambda: FakeJob.registry["bench"].st == "hot")
        status, body = chat(port)
        self.assertEqual(status, 200)
        self.assertEqual(body.count(b"data:"), 3)
        self.assertFalse(prod.sleeping)
        self.assertEqual(FakeJob.registry["bench"].st, "parked")
        wake = [e for e in events(r) if e["event"] == "request_wake"]
        self.assertTrue(wake and wake[0]["ok"])

    def test_concurrent_requests_wake_once(self):
        r, prod, gpu, port = self.mk()
        self.wait_for(lambda: prod.sleeping)
        res, errs = [], []

        def go():
            try:
                res.append(chat(port)[0])
            except Exception as e:      # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(res, [200] * 8)
        self.assertEqual(prod.calls.count("/wake_up"), 1)

    def test_stuck_job_is_killed_so_prod_still_wakes(self):
        r, prod, gpu, port = self.mk(stuck=True)
        self.wait_for(lambda: FakeJob.registry["bench"].st == "hot")
        self.assertEqual(chat(port)[0], 200)
        self.assertTrue(FakeJob.registry["bench"].killed)
        self.assertIn("job_killed", [e["event"] for e in events(r)])

    def test_a_slow_job_is_stopped_at_the_wake_deadline(self):
        # the job takes 10 s to step aside (its timeout is 30 s); a request may wait about wake_deadline_s, not 30 s
        r, prod, gpu, port = self.mk(job={"slow_park_s": 10, "timeout_s": 30}, policy={"wake_deadline_s": 1.0})
        self.wait_for(lambda: FakeJob.registry["bench"].st == "hot")
        t0 = time.time()
        self.assertEqual(chat(port)[0], 200)
        self.assertLess(time.time() - t0, 3.0)
        self.assertTrue(FakeJob.registry["bench"].killed)
        wake = [e for e in events(r) if e["event"] == "request_wake"][-1]
        self.assertTrue(wake["ok"])

    def test_warm_cmd_runs_in_the_background_after_a_wake(self):
        mark = os.path.join(tempfile.mkdtemp(), "warmed")
        r, prod, gpu, port = self.mk(service={"warm_cmd": f"sleep 0.3 && touch {mark}"})
        self.wait_for(lambda: FakeJob.registry["bench"].st == "hot")
        t0 = time.time()
        self.assertEqual(chat(port)[0], 200)
        self.assertLess(time.time() - t0, 0.3)                    # the request doesn't wait for the warm-up
        self.assertIsNotNone(r.status()["services"]["prod"]["warming_s"])
        self.wait_for(lambda: os.path.exists(mark))
        self.wait_for(lambda: "warm_done" in [e["event"] for e in events(r)])
        done = [e for e in events(r) if e["event"] == "warm_done"][-1]
        self.assertTrue(done["ok"])
        self.assertIsNone(r.status()["services"]["prod"]["warming_s"])

    def test_pause_wakes_prod_and_parks_job(self):
        r, prod, gpu, port = self.mk()
        self.wait_for(lambda: FakeJob.registry["bench"].st == "hot")
        open(r.top["pause_file"], "w").close()
        self.assertTrue(self.wait_for(lambda: not prod.sleeping and FakeJob.registry["bench"].st == "parked"))
        time.sleep(0.6)
        self.assertFalse(prod.sleeping)                          # paused: never parks again

    def test_shadow_touches_nothing_but_learns_and_logs(self):
        r, prod, gpu, port = self.mk(mode="shadow")
        time.sleep(0.8)
        self.assertFalse(prod.sleeping)
        self.assertEqual(FakeJob.registry["bench"].calls, [])
        would = [e for e in events(r) if e["event"] == "would"]
        self.assertTrue(would and would[0]["actions"][0]["kind"] == "park_service")
        n = len(r.svc["prod"]["gaps"])
        chat(port)
        self.assertEqual(len(r.svc["prod"]["gaps"]), n + 1)

    def test_health_probes_do_not_wake_or_count(self):
        r, prod, gpu, port = self.mk()
        self.assertTrue(self.wait_for(lambda: prod.sleeping))
        n = len(r.svc["prod"]["gaps"])
        for path in ("/health", "/v1/models", "/metrics"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path)
            c.getresponse().read()
            c.close()
        self.assertTrue(prod.sleeping)                       # nobody woke prod for a probe
        self.assertEqual(len(r.svc["prod"]["gaps"]), n)      # and probes are not activity
        self.assertEqual(r.svc["prod"]["inflight"], 0)

    def test_state_endpoint_and_status_file(self):
        r, prod, gpu, port = self.mk()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/gaprunner/state")
        s = json.loads(c.getresponse().read())
        self.assertEqual(s["mode"], "live")
        self.assertIn("prod", s["services"])
        r.save()
        self.assertTrue(os.path.exists(r.status_path))


if __name__ == "__main__":
    unittest.main()
