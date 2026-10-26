"""policy.max_active_jobs: at most N jobs on the GPU at once; the rest wait their turn in queue order."""
import unittest

from test_scheduler import LONG, POL, job, kinds, snap
from gaprunner import config as C
from gaprunner.scheduler import HOT, PARKED, Policy, decide, explain

ONE = Policy(**{**POL.__dict__, "max_active_jobs": 1})


class MaxActiveJobsTest(unittest.TestCase):
    def test_only_the_first_in_the_queue_starts(self):
        s = snap(svc_state=PARKED, jobs=[job("a", order=0), job("b", order=1)])
        self.assertEqual(kinds(decide(s, LONG, ONE)), [("resume_job", "a")])
        self.assertEqual(kinds(decide(s, LONG, POL)), [("resume_job", "a"), ("resume_job", "b")])   # no limit

    def test_a_waiting_job_says_why(self):
        s = snap(svc_state=PARKED, jobs=[job("a", state=HOT, order=0), job("b", order=1)])
        self.assertEqual(decide(s, LONG, ONE), [])
        word, why = explain(s, LONG, ONE)["b"]
        self.assertEqual(word, "waiting")
        self.assertIn("max active jobs reached (1 of 1", why)

    def test_lowering_the_limit_steps_aside_the_last_in_the_queue(self):
        s = snap(svc_state=PARKED, jobs=[job("a", state=HOT, order=1), job("b", state=HOT, order=0)])
        self.assertEqual(kinds(decide(s, LONG, ONE)), [("park_job", "a")])

    def test_the_service_stays_awake_when_no_job_could_start(self):
        s = snap(jobs=[job("a", state=HOT, gib=10, coexist="share", order=0), job("b", order=1)])
        self.assertNotIn(("park_service", "prod"), kinds(decide(s, LONG, ONE)))
        self.assertIn(("park_service", "prod"), kinds(decide(s, LONG, POL)))

    def test_config_wants_a_whole_number(self):
        base = {"service": [{"name": "prod", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:0", "gpu_gib": 1}]}
        self.assertEqual(C.parse({**base, "policy": {"max_active_jobs": 2}})["policy"].max_active_jobs, 2)
        for bad in (-1, 1.5):
            with self.assertRaises(C.ConfigError):
                C.parse({**base, "policy": {"max_active_jobs": bad}})


if __name__ == "__main__":
    unittest.main()
