"""A service that starts but never becomes ready (its engine can't load, or the GPU is still held by something else
when an exclusive window ends) is restarted at most max_restarts times in a row, not stopped and started forever."""
import tempfile
import unittest
from unittest.mock import patch

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner.runner import Runner


def runner(**svc):
    gpu = FakeGpu(100.0)
    prod = FakeVllm(gpu, "prod", 60.0)
    spec = {"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
            "stop_cmd": "true", "start_cmd": "true", "ready_timeout_s": 100}
    spec.update(svc)
    return Runner(C.parse({"gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()}, "service": [spec]}), gpu=gpu)


class RestartCapTest(unittest.TestCase):
    def cycle(self, r, now):
        """One failed attempt: the start times out, then a request's wake fails and the tick restarts it."""
        s = r.svc["prod"]
        with r.lock:
            if s["starting_since"] is not None:
                now[0] += 101
                with patch.object(s["driver"], "state", return_value="starting"):
                    r._windows()                       # ready_timeout_s passes: service_start_timeout
            s["dead_since"] = now[0]                    # what ensure_hot sets when the wake fails
            r._recover()

    def test_stops_restarting_after_max_restarts_and_alerts(self):
        r = runner()
        now = [100.0]
        r.clock = lambda: now[0]
        with patch.object(r, "_shell", return_value=True) as shell:
            for _ in range(6):
                self.cycle(r, now)
                now[0] += 61
        ev = [e for e in r.recent if e["event"].startswith("service_restart")]
        self.assertEqual([e["event"] for e in ev], ["service_restart"] * 3 + ["service_restart_gave_up"])
        self.assertEqual(shell.call_count, 6)          # three stop + start pairs, then no more commands
        self.assertIn("tried again", r.unavailable("prod") or "")

    def test_count_resets_when_it_comes_up(self):
        r = runner(max_restarts=2)
        now = [100.0]
        r.clock = lambda: now[0]
        s = r.svc["prod"]
        with patch.object(r, "_shell", return_value=True):
            self.cycle(r, now)
            self.assertEqual(s["restarts"], 1)
            with r.lock:
                r._windows()                             # the fake engine answers: ready
            self.assertEqual(s["restarts"], 0)
            for _ in range(2):
                self.cycle(r, now)
            self.assertEqual(s["restarts"], 2)
            self.assertTrue(r._restartable("prod") is False)


if __name__ == "__main__":
    unittest.main()
