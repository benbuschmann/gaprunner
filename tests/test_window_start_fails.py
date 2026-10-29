"""A service whose start_cmd fails when an exclusive window ends: requests get a 503 that says why, and GapRunner
restarts it at once instead of waiting for a request to fail its wake first."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fakes import FakeGpu, FakeVllm
from test_live import chat
from gaprunner import config as C
from gaprunner import gate
from gaprunner.gaps import GapModel
from gaprunner.runner import Runner


class WindowEndStartFailsTest(unittest.TestCase):
    def test_failed_recovery_keeps_503_and_retries_after_dead_retry(self):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        r = Runner(C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url,
                         "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "stop_cmd": "true", "start_cmd": "false", "dead_retry_s": 60}],
        }), gpu=gpu)
        now = [100.0]
        r.clock = lambda: now[0]
        s = r.svc["prod"]
        s["stopped"] = True
        with patch.object(r, "_shell", side_effect=lambda cmd, *args: cmd == "true") as shell:
            with r.lock:
                r._windows()
                r._recover()
            self.assertIsNotNone(r.unavailable("prod"))
            self.assertFalse(r.ensure_hot("prod"))
            self.assertEqual(shell.call_count, 3)  # window start, recovery stop and start
            with r.lock:
                r._recover()
            self.assertEqual(shell.call_count, 3)  # no command loop on every tick
            now[0] += 60
            shell.side_effect = None
            shell.return_value = True
            with r.lock:
                r._recover()
            self.assertEqual(shell.call_count, 5)
            self.assertIsNotNone(s["starting_since"])
            with r.lock:
                r._windows()  # fake service is ready
            self.assertIsNone(r.unavailable("prod"))

    def test_failed_start_at_window_end_is_reported_and_retried(self):
        d = tempfile.mkdtemp()
        down, tried = os.path.join(d, "prod-down"), os.path.join(d, "tried")
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0, down_file=down)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": d, "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0, "juggle_s": 0},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60,
                         "stop_cmd": f"touch {down}",
                         # fails the first time (the port is still held, say), works when tried again
                         "start_cmd": f"if [ -f {tried} ]; then rm -f {down}; else touch {tried}; exit 1; fi"}],
        })
        r = Runner(cfg, gpu=gpu)
        r.svc["prod"]["gaps"] = GapModel([600] * 50, min_samples=20)
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(r, "prod"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        r.tick()
        with open(r.top["window_file"], "w") as f:
            json.dump({"until": time.time() + 3600, "exclusive": True}, f)
        r.tick()
        self.assertTrue(r.svc["prod"]["stopped"])
        os.remove(r.top["window_file"])
        r.tick()                                              # the window ends; the first start_cmd fails
        ev = [e["event"] for e in r.recent]
        self.assertIn("service_restart", ev)                  # restarted at once, not after a failed request
        self.assertIsNotNone(r.unavailable("prod"))
        code, body = chat(srv.server_address[1])
        self.assertEqual(code, 503, body)
        self.assertNotIn("request_wake", [e["event"] for e in r.recent])     # no wake was tried for the request


if __name__ == "__main__":
    unittest.main()
