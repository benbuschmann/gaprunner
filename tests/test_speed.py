import unittest

from gaprunner import speed

METRICS = """# HELP vllm:generation_tokens_total Number of generation tokens processed.
vllm:generation_tokens_total{engine="0",model_name="a"} %s
vllm:generation_tokens_total{engine="1",model_name="a"} 0.0
vllm:prompt_tokens_total{engine="0",model_name="a"} %s
vllm:num_requests_running{engine="0",model_name="a"} 3.0
"""


class SpeedTest(unittest.TestCase):
    def test_parse_sums_label_sets(self):
        got = speed.parse(METRICS % ("9.7665e+04", "1.868319e+06"))
        self.assertEqual((got["out"], got["in"], got["running"]), (97665.0, 1868319.0, 3.0))
        self.assertIsNone(speed.parse("other_metric 1\n"))

    def test_rate_over_the_window(self):
        e, totals = {}, iter([(0, 0), (500, 5000), (1000, 10000), (1500, 15000)])
        reader = lambda url: dict(zip(("out", "in"), next(totals)))
        for t in (0, 10, 20, 30):
            speed.update(e, "http://x/metrics", t, True, reader)
        self.assertEqual(e["tps"]["out"], 50.0)
        self.assertEqual(e["tps"]["in"], 500.0)

    def test_restart_and_stop_reset(self):
        e, totals = {}, iter([(1000, 0), (2000, 0), (10, 0), (110, 0)])
        reader = lambda url: dict(zip(("out", "in"), next(totals)))
        for t in (0, 10, 20, 30):
            speed.update(e, "http://x/metrics", t, True, reader)
        self.assertEqual(e["tps"]["out"], 10.0)          # only the readings after the counter went back to zero
        speed.update(e, "http://x/metrics", 40, False, reader)
        self.assertIsNone(e["tps"])

    def test_url_defaults(self):
        self.assertEqual(speed.url_of({"driver": "vllm", "upstream": "http://127.0.0.1:18003/"}), "http://127.0.0.1:18003/metrics")
        self.assertIsNone(speed.url_of({"driver": "restart"}))
        self.assertEqual(speed.url_of({"metrics_url": "http://h/m"}), "http://h/m")


    def test_stats_cached_prompts_latency_and_idle(self):
        e, seq = {}, iter([
            {"out": 0, "in": 0, "in_cached": 0, "ttft_sum": 0, "ttft_n": 0, "e2e_sum": 0, "e2e_n": 0, "prefix_hits": 0, "prefix_queries": 0, "running": 1, "kv": 0.2},
            {"out": 3000, "in": 10000, "in_cached": 9000, "ttft_sum": 2, "ttft_n": 4, "e2e_sum": 40, "e2e_n": 4, "prefix_hits": 9000, "prefix_queries": 10000, "running": 2, "kv": 0.3},
            {"out": 3000, "in": 10000, "in_cached": 9000, "ttft_sum": 2, "ttft_n": 4, "e2e_sum": 40, "e2e_n": 4, "prefix_hits": 9000, "prefix_queries": 10000, "running": 0, "kv": 0.0}])
        reader = lambda url: next(seq)
        speed.update(e, "u", 0, True, reader)
        speed.update(e, "u", 30, True, reader)
        self.assertEqual(e["tps"]["out"], 100.0)
        self.assertEqual(e["tps"]["in"], round(1000 / 30, 1))   # only the 1,000 fresh prompt tokens
        st = e["stats"]
        self.assertEqual((st["ttft_s"], st["e2e_s"], st["prefix_hit"], st["cached_share"], st["running"]), (0.5, 10.0, 0.9, 0.9, 2))
        speed.update(e, "u", 60, True, reader)                 # idle now: tps drops, the last real speed is kept
        self.assertEqual(e["tps_last"]["out"], 100.0)



class FinderTest(unittest.TestCase):
    def test_unknown_servers_are_found_known_ones_skipped(self):
        from gaprunner.servers import Finder
        asked = []
        def ask(port):
            asked.append(port)
            return {18130: {"port": 18130, "models": ["my-model"], "owner": "vllm", "needs_key": False},
                    8080: {"port": 8080, "models": ["prod", "qwen3.8-27b"], "owner": "llama-swap", "needs_key": False}}.get(port)
        t = [0.0]
        f = Finder(scan=lambda: [8080, 18003, 18130, 22000], ask=ask, clock=lambda: t[0])
        got = f.refresh(known={18003}, skip_models=["qwen3.8-27b"])
        self.assertEqual([s["port"] for s in got], [18130])        # 18003 is prod's engine; 8080 lists prod's model
        self.assertNotIn(18003, asked)
        t[0] = 10.0
        f.refresh(known={18003})                                   # within SCAN_S: no new questions
        self.assertEqual(len(asked), 3)
