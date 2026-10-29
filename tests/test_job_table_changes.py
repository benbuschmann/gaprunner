"""Jobs added or withdrawn (the Jobs API) while another thread reads the job table: readers must not fail, and a
request the gate counted is always uncounted."""
import http.client
import unittest
import tempfile
import threading
from unittest.mock import patch

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C, gate
from gaprunner.runner import Runner

from test_live import chat


class JobTableChangeTest(unittest.TestCase):
    def setUp(self):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        self.addCleanup(prod.srv.server_close)
        self.addCleanup(prod.srv.shutdown)
        self.r = Runner(C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url,
                         "listen": "127.0.0.1:0", "gpu_gib": 60}],
            "job": [{"name": "bench", "driver": "fake", "gpu": gpu, "gpu_gib": 10}],
        }), gpu=gpu)
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(self.r, "prod"))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.port = srv.server_address[1]

    def test_gate_balances_count_when_activity_write_fails(self):
        with patch.object(self.r, "_activity", side_effect=OSError("write failed")):
            chat(self.port)
        self.assertEqual(self.r.svc["prod"]["inflight"], 0)

    def test_copied_readers_survive_withdrawal_during_queue_read(self):
        r = self.r
        for read, at in ((lambda: r.request_start("prod"), 1), (r.snapshot, 2), (r.status, 4)):
            with self.subTest(reader=read):
                j = r.job["bench"]
                real = C.queue_overrides
                calls = [0]
                def queue(top):
                    calls[0] += 1
                    if calls[0] == at:
                        r.job.pop("bench", None)
                    return real(top)
                try:
                    with patch.object(C, "queue_overrides", side_effect=queue):
                        read()
                finally:
                    r.job["bench"] = j
                    if r.svc["prod"]["inflight"]:
                        r.request_end("prod")

    def test_progress_does_not_park_a_withdrawn_job(self):
        r = self.r
        j = r.job["bench"]
        j.update(state="hot", saved_base="old", run_s=3600)
        j["spec"].update(saved_cmd="unused", max_hours=0.5)
        r._set_hold("bench", True)
        def saved(spec):
            r.job.pop("bench", None)
            return "new"
        with patch("gaprunner.progress.saved_point", side_effect=saved), patch.object(r, "_park_job") as park:
            r._track_progress()
        park.assert_not_called()

    def test_holders_survives_withdrawal_during_pid_read(self):
        r = self.r
        j = r.job["bench"]
        j["state"] = "hot"
        def pids():
            r.job.pop("bench")
            return []
        with patch.object(j["driver"], "pids", side_effect=pids):
            gib, _, _ = r.holders(None)
        self.assertEqual(gib["bench"], 10)


    def test_request_start_survives_a_job_added_meanwhile(self):
        r, real = self.r, self.r.coexist_of
        def coexist_of(name):                 # a Jobs API submit lands while request_start reads the queue file
            if "late" not in r.job:
                r.job["late"] = r._job_entry({**r.job["bench"]["spec"], "name": "late"})
            return real(name)
        r.coexist_of = coexist_of
        r.request_start("prod")
        r.request_end("prod")
        self.assertEqual(r.svc["prod"]["inflight"], 0)

    def test_gate_uncounts_a_request_when_request_start_fails(self):
        r = self.r
        def coexist_of(name):
            raise RuntimeError("boom")
        r.coexist_of = coexist_of
        try:
            chat(self.port)
        except (http.client.HTTPException, OSError):
            pass                              # the connection may drop; the count must still come back down
        self.assertEqual(r.svc["prod"]["inflight"], 0)

    def test_status_survives_a_job_withdrawn_meanwhile(self):
        r, real = self.r, self.r.snapshot
        r.job["late"] = r._job_entry({**r.job["bench"]["spec"], "name": "late"})
        def snapshot():
            snap = real()
            r.job.pop("late", None)           # a Jobs API withdraw lands between the snapshot and the job rows
            return snap
        r.snapshot = snapshot
        out = r.status()
        self.assertIn("bench", out["jobs"])
        self.assertNotIn("late", out["jobs"])


if __name__ == "__main__":
    unittest.main()
