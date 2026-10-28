"""Stop and withdraw wait for the job to exit without holding the transition lock: a wake, a tick or another action
can go ahead meanwhile, and the stopped job is not started again."""
import os
import sys
import tempfile
import threading
import time
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import drivers as D
from gaprunner.runner import Runner


def build(start_cmd):
    d = tempfile.mkdtemp()
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", 60.0)
    cfg = C.parse({
        "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
        "policy": {"reserve_gib": 0, "juggle_s": 0},
        "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60}],
        "job": [{"name": "slow", "driver": "restart", "gpu_gib": 10, "coexist": "share", "start_cmd": start_cmd}],
    })
    r = Runner(cfg, gpu=gpu)
    with r.lock:
        r._start("slow")
    pids = r.job["slow"]["driver"].pids()
    assert pids, "the job didn't start"
    return r, pids


def lock_free_while(r, busy, timeout=1.0):
    """True if the lock can be taken while busy() is still true."""
    time.sleep(0.5)                                   # let the stop send SIGTERM and start waiting
    if not busy():
        return False
    got = r.lock.acquire(timeout=timeout)
    if got:
        r.lock.release()
    return got and busy()


class StopWithoutLockTest(unittest.TestCase):
    def test_stop_waits_without_the_lock_and_still_kills(self):
        r, pids = build("trap '' TERM; sleep 60")     # ignores SIGTERM: killed after 15 s
        self.addCleanup(lambda: [os.kill(p, 9) for p in pids if D.alive(p)])
        t = threading.Thread(target=r.stop_job, args=("slow",), daemon=True)
        t.start()
        self.assertTrue(lock_free_while(r, t.is_alive))
        r.tick()                                      # a tick during the wait: it must not start the job again
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertFalse(any(D.alive(p) for p in pids))
        r.tick()
        self.assertEqual(r.job["slow"]["state"], "parked")
        self.assertFalse(r.job["slow"]["driver"].pids())
        ev = [e["event"] for e in r.recent]
        self.assertNotIn("start", ev[ev.index("stop"):])                       # not started again

    def test_withdraw_waits_without_the_lock(self):
        r, pids = build("trap 'sleep 2; exit 0' TERM; sleep 60")              # takes 2 s to exit on SIGTERM
        self.addCleanup(lambda: [os.kill(p, 9) for p in pids if D.alive(p)])
        t = threading.Thread(target=r.remove_job, args=("slow",), daemon=True)
        t.start()
        self.assertTrue(lock_free_while(r, t.is_alive))
        t.join(20)
        self.assertNotIn("slow", r.job)
        self.assertFalse(any(D.alive(p) for p in pids))


if __name__ == "__main__":
    unittest.main()
