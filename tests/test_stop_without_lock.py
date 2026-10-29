"""Stop and withdraw release the transition lock, but own the old run until the wait finishes."""
import os
import shlex
import signal
import tempfile
import threading
import unittest
import time
from unittest.mock import Mock, patch

from fakes import FakeGpu
from gaprunner import config as C, runner as R, ui


class StopWithoutLockTest(unittest.TestCase):
    def exercise(self, withdraw=False, start=False):
        gpu = FakeGpu(100.0)
        r = R.Runner(C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "driver": "fake", "upstream": "http://127.0.0.1:9",
                         "listen": "127.0.0.1:0", "gpu_gib": 60, "gpu": gpu, "start": "hot"}],
            "policy": {"reserve_gib": 0, "juggle_s": 0},
            "job": [{"name": "slow", "driver": "fake", "gpu_gib": 10,
                     "coexist": "share", "park_s": 0.1, "gpu": gpu, "start": "hot"}],
        }), gpu=gpu)
        j = r.job["slow"]
        j["state"] = "hot"
        waiting, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        now, alive, signals, errors, starts = [0.0], [True], [], [], []

        def sleep(seconds):
            waiting.set()
            if not release.wait(3):
                raise AssertionError("test did not release the stop wait")
            now[0] += seconds

        def send(sig):
            signals.append((sig, now[0]))
            if sig == signal.SIGKILL:
                alive[0] = False
                j["driver"].st = "parked"

        def run():
            try:
                (r.remove_job if withdraw else r.stop_job)("slow")
            except Exception as exc:
                errors.append(exc)

        with patch.object(R, "time", Mock(wraps=time, monotonic=lambda: now[0], sleep=sleep)), \
             patch.object(R.D, "alive", side_effect=lambda pid: alive[0]), \
             patch.object(j["driver"], "pids", side_effect=lambda: [123] if alive[0] else []), \
             patch.object(j["driver"], "signal", side_effect=send), \
             patch.object(j["driver"], "resume", side_effect=lambda: starts.append(True)):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            try:
                self.assertTrue(waiting.wait(2), errors)
                got = r.lock.acquire(timeout=0.2)
                if got:
                    r.lock.release()
                self.assertTrue(got, "Stop holds the transition lock during its wait")
                if start:
                    ui.act(r, {"action": "set", "name": "slow", "field": "hold", "value": False})
                    # A wake can park the old run while Stop is waiting on its remaining processes.
                    j["driver"].st = "parked"
                r.tick()
                self.assertEqual(starts, [], "Start must wait until Stop/withdraw finishes")
            finally:
                release.set()
                t.join(3)
            self.assertFalse(t.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([s for s, _ in signals], [signal.SIGTERM, signal.SIGKILL])
            self.assertGreaterEqual(signals[-1][1], 15)
            self.assertLess(signals[-1][1], 15.3)
            self.assertFalse(alive[0])
            if withdraw:
                self.assertNotIn("slow", r.job)
                self.assertNotIn("slow", C.queue_overrides(r.top))
            else:
                r.tick()
                self.assertEqual(starts, [True] if start else [])

    def test_stop_kills_a_real_process_that_ignores_term(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        ready = os.path.join(folder.name, "ready")
        gpu = FakeGpu(100.0)
        r = R.Runner(C.parse({
            "gaprunner": {"mode": "live", "state_dir": folder.name},
            "service": [{"name": "prod", "driver": "fake", "gpu": gpu,
                         "upstream": "http://127.0.0.1:9", "listen": "127.0.0.1:0", "gpu_gib": 60}],
            "job": [{"name": "slow", "driver": "restart", "gpu_gib": 10,
                     "start_cmd": f"trap '' TERM; touch {shlex.quote(ready)}; exec sleep 60"}],
        }), gpu=gpu)
        with r.lock:
            self.assertTrue(r._start("slow"))
        proc = r.job["slow"]["proc"]
        def cleanup():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 3
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(os.path.exists(ready), "the SIGTERM handler was not installed")
        now = [0.0]
        def sleep(seconds):
            self.assertIsNone(proc.poll(), "the process should ignore SIGTERM")
            now[0] += seconds
        with patch.object(R, "time", Mock(wraps=time, monotonic=lambda: now[0], sleep=sleep)):
            r.stop_job("slow")
        self.assertEqual(proc.wait(timeout=3), -signal.SIGKILL)
        self.assertGreaterEqual(now[0], 15)
        self.assertFalse(r.job["slow"]["driver"].pids())

    def test_stop_waits_without_the_lock_and_still_kills(self):
        self.exercise()

    def test_withdraw_waits_without_the_lock(self):
        self.exercise(withdraw=True)

    def test_start_during_stop_waits_for_the_old_run(self):
        self.exercise(start=True)

    def test_start_during_withdraw_cannot_restart_the_job(self):
        self.exercise(withdraw=True, start=True)


if __name__ == "__main__":
    unittest.main()
