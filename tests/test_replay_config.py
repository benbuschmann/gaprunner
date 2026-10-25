import os
import tempfile
import unittest

from gaprunner import config as C
from gaprunner import replay

CFG = {
    "gaprunner": {"state_dir": tempfile.mkdtemp(), "gpu_total_gib": 250},
    "policy": {"p_min": 0.75, "worth_factor": 3, "reserve_gib": 4},
    "service": [{"name": "prod", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:1", "gpu_gib": 200,
                 "idle_park_s": 30, "wake_s": 1.2}],
    "job": [{"name": "bench", "driver": "command", "gpu_gib": 40, "park_s": 0.1, "resume_s": 1.5}],
}


def trace(n=200, busy=10, short=20, long_=900):
    """Alternating short and long idle gaps."""
    ev, t = [], 1_700_000_000.0
    for i in range(n):
        ev += [(t, "start"), (t + busy, "end")]
        t += busy + (long_ if i % 4 == 0 else short)
    return ev


class ReplayTest(unittest.TestCase):
    def test_runs_jobs_in_long_gaps_only(self):
        cfg = C.parse(CFG)
        out = replay.simulate(cfg, [trace()])
        self.assertGreater(out["total_job_hours_per_day"], 0)
        self.assertGreater(out["gap_samples"], 100)
        self.assertLessEqual(out["added_wait_s"]["max"], 0.1 + 1.2 + 1e-9)
        eager = replay.simulate(cfg, [trace()], policy=C.Policy(p_min=0.0, worth_factor=0, reserve_gib=4))
        self.assertGreater(eager["service_wakes_per_day"], out["service_wakes_per_day"])

    def test_yield_format(self):
        p = os.path.join(tempfile.mkdtemp(), "y.log")
        open(p, "w").write("2026-10-23T10:00:00Z FREEZE seat\n2026-10-23T10:00:30Z THAW GLM idle 5s\n"
                           "2026-10-23T10:10:00Z FREEZE seat\n")
        [ev] = replay.load_yield([p])
        self.assertEqual([e for _, e in ev], ["start", "end", "start"])
        self.assertEqual(ev[1][0] - ev[0][0], 25)


class ConfigTest(unittest.TestCase):
    def test_errors(self):
        with self.assertRaises(C.ConfigError):
            C.parse({"service": []})
        with self.assertRaises(C.ConfigError):
            C.parse({**CFG, "policy": {"nope": 1}})
        with self.assertRaises(C.ConfigError):
            C.parse({**CFG, "gaprunner": {"windows": ["25:00-x"]}})

    def test_windows_and_queue(self):
        top = C.parse({**CFG, "gaprunner": {"windows": ["22:00-06:00"], "state_dir": tempfile.mkdtemp()}})["gaprunner"]
        import datetime as dt
        self.assertTrue(C.window_open(top, dt.datetime(2026, 1, 1, 23, 0)))
        self.assertFalse(C.window_open(top, dt.datetime(2026, 1, 1, 12, 0)))
        open(top["window_file"], "w").write(str(dt.datetime(2026, 1, 1, 13, 0).timestamp()))
        self.assertTrue(C.window_open(top, dt.datetime(2026, 1, 1, 12, 0)))       # UI window until 13:00
        self.assertFalse(C.window_open(top, dt.datetime(2026, 1, 1, 14, 0)))
        q = os.path.join(tempfile.mkdtemp(), "q.json")
        open(q, "w").write('{"jobs": [{"name": "b", "hold": true}, {"name": "a"}]}')
        self.assertEqual(C.queue_overrides({"queue_file": q}),
                         {"b": {"order": 0, "hold": True, "window_only": False, "coexist": None, "after": None, "tags": None},
                          "a": {"order": 1, "hold": False, "window_only": False, "coexist": None, "after": None, "tags": None}})


if __name__ == "__main__":
    unittest.main()


class JobColorTest(unittest.TestCase):
    def test_a_recipe_can_name_its_colour_from_the_palette_only(self):
        base = {"service": [{"name": "p", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:1", "gpu_gib": 1}]}
        job = {"name": "j", "driver": "restart", "gpu_gib": 1, "start_cmd": "true"}
        self.assertEqual(C.parse({**base, "job": [{**job, "color": "orange"}]})["jobs"][0]["color"], "orange")
        with self.assertRaises(C.ConfigError):
            C.parse({**base, "job": [{**job, "color": "mauve"}]})
