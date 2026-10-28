import os
import tempfile
import threading
import types
import unittest

from gaprunner.runner import Runner
from gaprunner.scheduler import HOT


class WindDownTest(unittest.TestCase):
    """A wound-down job with saved_cmd stops when its saved point moves on, not before."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "saved")
        open(self.f, "w").write("6\n")
        self.parked, self.events = [], []
        self.r = types.SimpleNamespace(lock=threading.RLock(), _park_job=lambda n, reason: self.parked.append(reason),
                                       audit=lambda ev, **k: self.events.append(ev))
        self.j = {"spec": {"saved_cmd": f"cat {self.f}", "workdir": self.d}, "state": HOT, "frozen": False}

        self.r.job = {"bench": self.j}

    def tick(self, now, held=True):
        Runner._wind_down_at_saved_point(self.r, "bench", self.j, held, now)

    def test_stops_at_the_next_saved_point(self):
        self.tick(0)                                   # winding down starts at saved point 6
        self.tick(20)
        self.assertEqual(self.parked, [])
        open(self.f, "w").write("7\n")
        self.tick(30)                                  # within 15 s of the last read: not read yet
        self.assertEqual(self.parked, [])
        self.tick(40)
        self.assertEqual(len(self.parked), 1)
        self.assertIn("saved point 7", self.parked[0])
        self.assertEqual(self.events, ["wound_down"])

    def test_not_wound_down_or_frozen_does_nothing(self):
        self.tick(0, held=False)
        open(self.f, "w").write("7\n")
        self.tick(20, held=False)
        self.j["frozen"] = True
        self.tick(40)
        self.assertEqual(self.parked, [])
        self.assertIsNone(self.j["saved_base"])
