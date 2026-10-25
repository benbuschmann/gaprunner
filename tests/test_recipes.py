"""Jobs started from their recipe (start_cmd): finish, fail, restart after a park, and yield to requests."""
import os
import subprocess
import sys
import threading
import time
import unittest

from test_live import chat
from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import gate
from gaprunner.gaps import GapModel
from gaprunner.runner import Runner
import tempfile

PY = sys.executable


def build(job, prod_gib=60.0):
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", prod_gib)
    cfg = C.parse({
        "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp(), "tick_s": 0.05, "verify_free_s": 0.5},
        "policy": {"p_min": 0.5, "worth_factor": 1, "juggle_s": 0, "reserve_gib": 0},
        "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": prod_gib,
                     "idle_park_s": 0.3, "wake_s": 0.05}],
        "job": [{"name": "j", "driver": "restart", "park_s": 0.1, "resume_s": 0.1, "timeout_s": 2, **job}],
    })
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
    return r, prod, srv.server_address[1], stop


def wait_for(cond, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def proc_state(pid):
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


class RecipeTest(unittest.TestCase):
    def tearDown(self):
        self.stop.set()
        for e in self.r.job.values():
            e["driver"].kill()

    def test_runs_beside_prod_to_completion_and_is_not_restarted(self):
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "share", "start_cmd": f"{PY} -c 'import time; time.sleep(0.4)'"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "done"))
        self.assertFalse(prod.sleeping)                       # it ran beside prod; prod never parked
        time.sleep(0.5)
        self.assertEqual(self.r.job["j"]["state"], "done")
        self.assertEqual(sum(1 for e in self.r.recent if e["event"] == "start" and e.get("target") == "j"), 1)

    def test_finish_while_gaprunner_is_down_is_still_seen_as_done(self):
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "share", "start_cmd": f"{PY} -c 'import time; time.sleep(0.6)'"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "hot"))
        self.stop.set()                                         # GapRunner "goes down" while the job runs
        self.r.job["j"]["proc"].wait()                          # the job finishes unobserved (a dead GapRunner's
                                                                # children are reaped by init; do that here)
        again = Runner(self.r.cfg, gpu=self.r.gpu)              # restart with the same state directory
        again.refresh()
        self.assertEqual(again.job["j"]["state"], "done")
        self.assertEqual(again.job["j"]["result"]["rc"], 0)
        self.r = again

    def test_child_processes_are_paused_and_stopped_with_the_job(self):
        cmd = f"{PY} -c 'import subprocess,time; subprocess.Popen([\"sleep\", \"60\"]); time.sleep(60)'"
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "yield", "yield_resume_s": 0.3, "start_cmd": cmd})
        self.assertTrue(wait_for(lambda: len(self.r.job["j"]["driver"].pids()) >= 3))   # wrapper, python, sleep
        pids = self.r.job["j"]["driver"].pids()
        self.r.request_start("prod")
        self.assertTrue(all(proc_state(p).startswith("T") for p in pids))
        self.r.request_end("prod")
        self.r.job["j"]["driver"].kill()
        self.assertTrue(wait_for(lambda: not self.r.job["j"]["driver"].pids()))

    def test_a_slow_launcher_counts_as_running(self):
        # the recipe finds its processes by pgrep, and the launcher takes a moment before they exist
        marker = f"gaprunner-slow-launch-{id(self)}"
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "share", "pgrep": marker,
                                               "start_cmd": f"sleep 1; exec {PY} -c 'import time; time.sleep(60)' {marker}"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["proc"] is not None))
        self.assertEqual(self.r.job["j"]["state"], "hot")                     # launching, not "parked"
        self.assertTrue(wait_for(lambda: bool(self.r.job["j"]["driver"].pids())))
        self.assertEqual(sum(1 for e in self.r.recent if e["event"] == "start"), 1)
        # processes in the job's group count as the job's even when the recipe's pgrep doesn't match them
        child = subprocess.Popen(["sleep", "30"], start_new_session=False)
        self.addCleanup(child.kill)
        wrapper = int(open(self.r.job["j"]["spec"]["wrapper_pid_file"]).read().split()[0])
        self.assertIn(wrapper, self.r.job["j"]["driver"].pids())

    def test_a_name_match_on_another_command_is_not_the_job(self):
        marker = f"gaprunner-false-match-{id(self)}"
        bystander = subprocess.Popen([PY, "-c", "import time; time.sleep(30)", marker])   # e.g. someone's shell
        self.addCleanup(bystander.kill)
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "share", "hold": True, "pgrep": marker,
                                               "start_cmd": f"{PY} -c 'import time; time.sleep(60)'"})
        time.sleep(0.4)
        self.assertEqual(self.r.job["j"]["driver"].pids(), [])        # not launched: nothing is the job
        self.assertEqual(self.r.job["j"]["state"], "parked")
        self.assertIsNone(bystander.poll())

    def test_freeze_keeps_it_stopped_through_prod_traffic_and_thaw_continues(self):
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "yield", "yield_resume_s": 0.2,
                                               "start_cmd": f"{PY} -c 'import time; time.sleep(60)'"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "hot"))
        pids = self.r.job["j"]["driver"].pids()
        self.r.freeze("j")
        self.assertTrue(all(proc_state(p).startswith("T") for p in pids))
        self.r.request_start("prod"); self.r.request_end("prod")        # prod traffic would normally thaw a yield job
        time.sleep(0.8)
        self.assertTrue(all(proc_state(p).startswith("T") for p in pids))
        self.assertEqual(self.r.status()["jobs"]["j"]["word"], "frozen")
        self.r.freeze("j", on=False)
        self.assertTrue(wait_for(lambda: not proc_state(pids[0]).startswith("T")))

    def test_a_launched_job_that_sleeps_is_woken_through_its_driver(self):
        d = tempfile.mkdtemp()
        st = os.path.join(d, "state")
        self.r, prod, port, self.stop = build({"driver": "command", "gpu_gib": 10, "coexist": "share", "hold": True,
                                               "start_cmd": f"{PY} -c 'import time; time.sleep(60)'",
                                               "park_cmd": f"echo parked > {st}", "resume_cmd": f"echo hot > {st}",
                                               "state_cmd": f"cat {st} 2>/dev/null || echo hot"})
        self.stop.set()                                         # drive it by hand
        with self.r.lock:
            self.assertTrue(self.r._start("j"))
            self.r._park_job("j", "test")                         # sleeps; the launcher stays alive
            self.assertEqual(self.r.job["j"]["driver"].state(), "parked")
            self.assertTrue(self.r._resume_job("j", "test"))
        self.assertEqual(open(st).read().strip(), "hot")         # resume_cmd ran

    def test_failure_is_reported_not_retried(self):
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "share", "start_cmd": f"{PY} -c 'raise SystemExit(3)'"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "failed"))
        self.assertEqual(self.r.job["j"]["result"]["rc"], 3)
        self.assertIn("exited with an error", self.r.status()["jobs"]["j"]["why"])

    def test_gap_job_is_stopped_by_a_request_and_restarted_in_the_next_gap(self):
        self.r, prod, port, self.stop = build({"gpu_gib": 70, "start_cmd": f"{PY} -c 'import time; time.sleep(60)'"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "hot"))
        first = self.r.job["j"]["driver"].pids()
        self.assertTrue(prod.sleeping)
        self.assertEqual(chat(port)[0], 200)
        self.assertEqual(self.r.job["j"]["state"], "parked")   # stopped by the park, not "failed"
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "hot" and self.r.job["j"]["driver"].pids() != first))

    def test_yield_job_pauses_while_prod_is_busy(self):
        self.r, prod, port, self.stop = build({"gpu_gib": 10, "coexist": "yield", "yield_resume_s": 0.3,
                                               "start_cmd": f"{PY} -c 'import time; time.sleep(60)'"})
        self.assertTrue(wait_for(lambda: self.r.job["j"]["state"] == "hot"))
        pid = self.r.job["j"]["driver"].pids()[0]
        self.r.request_start("prod")
        self.assertTrue(proc_state(pid).startswith("T"))       # stopped while the request is in flight
        self.r.request_end("prod")
        self.assertTrue(wait_for(lambda: not proc_state(pid).startswith("T")))
        self.assertFalse(prod.sleeping)                         # prod never parked for it


if __name__ == "__main__":
    unittest.main()
