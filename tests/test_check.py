import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class CheckTest(unittest.TestCase):
    """`gaprunner check` writes nothing; --strict fails when the GPU or a service isn't reachable."""

    def run_check(self, *extra):
        d = tempfile.mkdtemp()
        state = os.path.join(d, "state")
        os.makedirs(state)
        audit = os.path.join(state, "audit.jsonl")
        with open(audit, "w") as f:
            f.write('{"t": 1, "event": "old"}\n')                     # older than any keep window
        cfg = os.path.join(d, "c.toml")
        with open(cfg, "w") as f:
            f.write(f"""[gaprunner]
state_dir = "{state}"
gpu_index = "999"

[[service]]
name = "prod"
driver = "vllm"
upstream = "http://127.0.0.1:9"
listen = "127.0.0.1:0"
gpu_gib = 10
""")
        before = sorted(os.listdir(state))
        r = subprocess.run([sys.executable, "-m", "gaprunner", "check", cfg, *extra], capture_output=True, text=True,
                           cwd=ROOT, timeout=120)
        return r, state, audit, before

    def test_check_writes_nothing(self):
        r, state, audit, before = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(state)), before)
        self.assertIn('"event": "old"', open(audit).read())          # not trimmed
        self.assertIn("not ready", r.stderr)

    def test_strict_fails_when_not_ready(self):
        r, *_ = self.run_check("--strict")
        self.assertEqual(r.returncode, 1, r.stderr)
