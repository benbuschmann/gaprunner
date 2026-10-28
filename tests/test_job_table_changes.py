"""Jobs added or withdrawn (the Jobs API) while another thread reads the job table: readers must not fail, and a
request the gate counted is always uncounted."""
import http.client
import unittest

from test_live import build, chat


class JobTableChangeTest(unittest.TestCase):
    def setUp(self):
        self.r, prod, gpu, srv, stop = build()
        self.addCleanup(stop.set)
        self.port = srv.server_address[1]

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
