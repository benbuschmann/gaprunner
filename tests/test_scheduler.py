import unittest

from gaprunner.gaps import GapModel
from gaprunner.scheduler import HOT, PARKED, Job, Policy, Service, Snapshot, decide, explain, plan_wake

POL = Policy(p_min=0.75, worth_factor=3, max_added_wait_s=5, juggle_s=15, starvation_s=3600, reserve_gib=4,
             cold_start_after_s=120)
LONG = {"prod": GapModel([600] * 100, min_samples=20)}      # every gap lasts 10 minutes
SHORT = {"prod": GapModel([20] * 100, min_samples=20)}      # every gap lasts 20 s


def snap(now=1000, svc_state=HOT, last_done=900, jobs=(), window=False, paused=False, used=None):
    return Snapshot(now, [Service("prod", svc_state, 0, last_done, 60, 30, 1.0)], list(jobs), 100, used, window, paused)


def job(name="bench", state=PARKED, gib=30, park=0.5, resume=1.0, order=0, **kw):
    return Job(name, state, gib, park, resume, order, **kw)


def kinds(acts):
    return [(a.kind, a.target) for a in acts]


class SchedulerTest(unittest.TestCase):
    def test_idle_service_parks_only_when_a_job_is_worth_it(self):
        self.assertEqual(kinds(decide(snap(jobs=[job()]), LONG, POL)), [("park_service", "prod")])
        self.assertEqual(decide(snap(jobs=[job()]), SHORT, POL), [])          # gap too short to pay the swap
        self.assertEqual(decide(snap(jobs=[]), LONG, POL), [])                # nothing to run: stay hot
        self.assertEqual(decide(snap(last_done=990, jobs=[job()]), LONG, POL), [])   # not idle long enough

    def test_admit_after_service_parked(self):
        acts = decide(snap(svc_state=PARKED, jobs=[job()]), LONG, POL)
        self.assertEqual(kinds(acts), [("resume_job", "bench")])
        self.assertIn("of quiet", acts[0].reason)

    def test_hold_window_and_reclaim_limit(self):
        s = lambda **k: snap(svc_state=PARKED, **k)
        self.assertEqual(decide(s(jobs=[job(hold=True)]), LONG, POL), [])
        self.assertEqual(decide(s(jobs=[job(window_only=True)]), LONG, POL), [])
        self.assertEqual(kinds(decide(s(jobs=[job(window_only=True)], window=True), LONG, POL)), [("resume_job", "bench")])
        slow = job(park=22, resume=16)                                        # reclaim 23 s > 5 s: windows only
        self.assertEqual(decide(s(jobs=[slow]), LONG, POL), [])
        self.assertEqual(kinds(decide(s(jobs=[slow], window=True), LONG, POL)), [("resume_job", "bench")])

    def test_memory_order_juggle_and_starvation(self):
        a, b = job("a", gib=60, order=0), job("b", gib=60, order=1)
        self.assertEqual(kinds(decide(snap(svc_state=PARKED, jobs=[a, b]), LONG, POL)), [("resume_job", "a")])
        b.waiting_since = 1000 - 4000                                         # starving: jumps the queue
        self.assertEqual(kinds(decide(snap(svc_state=PARKED, jobs=[a, b]), LONG, POL)), [("resume_job", "b")])
        c = job("c", parked_at=995)                                           # parked 5 s ago: anti-thrash
        self.assertEqual(decide(snap(svc_state=PARKED, jobs=[c]), LONG, POL), [])
        self.assertEqual(decide(snap(svc_state=PARKED, jobs=[job(gib=30)], used=80), LONG, POL), [])  # driver says full

    def test_cold_start_without_history(self):
        empty = {"prod": GapModel()}
        self.assertEqual(decide(snap(svc_state=PARKED, last_done=950, jobs=[job()]), empty, POL), [])
        self.assertEqual(kinds(decide(snap(svc_state=PARKED, last_done=800, jobs=[job()]), empty, POL)),
                         [("resume_job", "bench")])

    def test_pause_and_plan_wake(self):
        s = snap(svc_state=PARKED, jobs=[job(state=HOT)], paused=True)
        self.assertEqual(kinds(decide(s, LONG, POL)), [("park_job", "bench"), ("wake_service", "prod")])
        self.assertEqual(plan_wake(snap(jobs=[job("x", state=HOT), job("y")])), ["x"])

    def test_small_jobs_run_beside_a_hot_service(self):
        # prod holds 60 of 100 GiB, reserve 4: a 30 GiB coexist job fits beside it, a 40 GiB one does not
        small, big = job("small", gib=30, coexist="share"), job("big", gib=40, coexist="share", order=1)
        acts = decide(snap(last_done=995, jobs=[small, big]), SHORT, POL)       # prod busy-ish, gaps short
        self.assertEqual(kinds(acts), [("resume_job", "small")])
        self.assertIn("beside", acts[0].reason)
        plain = job("plain", gib=30)                                           # coexist "no": needs a gap
        self.assertEqual(decide(snap(last_done=995, jobs=[plain]), SHORT, POL), [])

    def test_beside_jobs_share_the_reclaim_budget_while_prod_sleeps(self):
        # prod (60 GiB) asleep; five 15 GiB share jobs with 2 s parks: 40 GiB fits beside awake prod (two jobs),
        # the rest would have to step aside on wake, 2 s each, within 5 s minus prod's 1 s wake: only one more
        jobs = [job(f"j{i}", gib=15, park=2.0, coexist="share", order=i) for i in range(5)]
        acts = decide(snap(svc_state=PARKED, jobs=jobs), LONG, POL)
        self.assertEqual(len([a for a in acts if a.kind == "resume_job"]), 4)

    def test_wake_parks_only_what_does_not_fit(self):
        jobs = [job("gapjob", state=HOT, gib=20), job("a", state=HOT, gib=20, coexist="share", order=1),
                job("b", state=HOT, gib=30, coexist="yield", order=2)]
        s = snap(svc_state=PARKED, jobs=jobs)
        # prod needs 60; 100 - 4 reserve = 96: keep a (20), park b (30) and the gap job
        self.assertEqual(sorted(plan_wake(s, "prod", reserve_gib=4)), ["b", "gapjob"])
        self.assertEqual(sorted(plan_wake(s)), ["a", "b", "gapjob"])

    def test_explain_in_words(self):
        jobs = [job("run", state=HOT), job("held", hold=True), job("slow", park=22, resume=16), job("ok")]
        e = explain(snap(jobs=jobs), LONG, POL)
        self.assertEqual(e["run"][0], "running")
        self.assertEqual(e["held"], ("held", "wound down: press Start to let it run"))
        self.assertEqual(e["slow"][0], "window")
        self.assertIn("prod would wait", e["slow"][1])
        self.assertEqual(e["ok"][0], "next")
        self.assertEqual(explain(snap(jobs=[job()]), SHORT, POL)[job().name][0], "waiting")


if __name__ == "__main__":
    unittest.main()
