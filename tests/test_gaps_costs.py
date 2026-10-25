import os
import tempfile
import unittest

from gaprunner.costs import CostBook
from gaprunner.gaps import GapModel
from gaprunner import progress


class GapTest(unittest.TestCase):
    def test_conditional_survival(self):
        m = GapModel([10] * 50 + [100] * 50, min_samples=10)
        self.assertAlmostEqual(m.p_continue(0, 20), 0.5)       # half the gaps last 20 s
        self.assertAlmostEqual(m.p_continue(15, 20), 1.0)      # past 15 s only the long gaps remain
        self.assertEqual(m.median_remaining(15), 85)

    def test_too_little_history(self):
        self.assertIsNone(GapModel([5, 6], min_samples=20).p_continue(0, 1))

    def test_beyond_data_uses_longest_well_observed(self):
        m = GapModel(list(range(1, 101)), min_samples=10)
        self.assertEqual(m.p_continue(500, 5), m.p_continue(91, 5))

    def test_window_and_persistence(self):
        m = GapModel(range(10), max_samples=5, min_samples=1)
        self.assertEqual(m.sorted, [5, 6, 7, 8, 9])
        p = os.path.join(tempfile.mkdtemp(), "g.json")
        m.save(p)
        self.assertEqual(GapModel.load(p, min_samples=1).sorted, [5, 6, 7, 8, 9])
        self.assertEqual(len(GapModel.load(p + ".missing")), 0)


class CostTest(unittest.TestCase):
    def test_seed_then_measured_median(self):
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        c = CostBook(p, keep=3)
        c.seed("prod", "resume", 1.2)
        self.assertEqual(c.get("prod", "resume"), (1.2, False))
        for s in (5, 1, 2, 3):
            c.record("prod", "resume", s)
        self.assertEqual(c.get("prod", "resume"), (2, True))      # last 3: 1, 2, 3
        self.assertEqual(CostBook(p).get("prod", "resume"), (2, True))
        self.assertTrue(c.table()["prod"]["resume"]["measured"])


if __name__ == "__main__":
    unittest.main()


class ProgressTest(unittest.TestCase):
    def test_parse_and_rate(self):
        from gaprunner import progress
        self.assertEqual(progress.parse("step 12/50"), 0.24)
        self.assertEqual(progress.parse("0% overall, stage 1/3 search"), 0.0)     # the first number wins
        self.assertEqual(progress.parse("6/50 steps (12%)"), 0.12)
        self.assertEqual(progress.parse("eval 30.5 %"), 0.305)
        self.assertEqual(progress.parse("0.5"), 0.5)
        self.assertIsNone(progress.parse("warming up"))
        r = progress.rate([(0, 0.1), (1800, 0.2)])          # 10% per half hour of running
        self.assertAlmostEqual(r, 0.2)
        self.assertEqual(progress.hours_left(0.2, r), 4.0)
        import tempfile, os
        d = tempfile.mkdtemp()
        f, sub, sub_text = progress.read_both({"progress_cmd": "printf '6/50 steps\\nrollouts 18/32\\n'", "workdir": d})[0], *progress.read_both({"progress_cmd": "printf '6/50 steps\\nrollouts 18/32\\n'", "workdir": d})[2:]
        self.assertEqual((f, sub, sub_text), (0.12, 0.5625, "rollouts 18/32"))



class SummaryTest(unittest.TestCase):
    def test_summary_lines(self):
        self.assertEqual(progress.summary({"summary_cmd": "printf 'best:\\n  trial 63\\n\\n'"}), ["best:", "  trial 63"])
        self.assertIsNone(progress.summary({"summary_cmd": "exit 3"}))
