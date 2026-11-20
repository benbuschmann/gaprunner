"""What the engine receives after the gate's per-engine request fixes: model_name, and a reasoning effort translated by
reasoning_effort_map / reasoning_effort_default (two engines can read the same effort name differently)."""
import http.client
import json
import tempfile
import threading
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import gate
from gaprunner.runner import Runner

E_MAP = {"low": 25, "high": 50, "xhigh": 75, "max": 100}


class GateRewriteTest(unittest.TestCase):
    def start(self, **engine):
        gpu = FakeGpu(100.0)
        self.fake = FakeVllm(gpu, "prod", 60.0)
        self.addCleanup(self.fake.srv.server_close)
        self.addCleanup(self.fake.srv.shutdown)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "listen": "127.0.0.1:0", "driver": "vllm", "upstream": self.fake.url, "gpu_gib": 60,
                         "model_name": "served-model", **engine}],
        })
        r = Runner(cfg, gpu=gpu)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def loop():                       # the daemon's tick: it finds the service hot so the gate forwards
            while not stop.is_set():
                r.tick()
                stop.wait(0.05)
        threading.Thread(target=loop, daemon=True).start()
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(r, "prod"))
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.port = srv.server_address[1]

    def sent(self, body, path="/v1/chat/completions"):
        """POST body through the gate; the JSON the engine received."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        c.getresponse().read()
        c.close()
        got, raw = self.fake.bodies[-1]
        self.assertEqual(got, path)
        return json.loads(raw)

    def test_effort_names_become_the_engines_values(self):
        self.start(reasoning_effort_map=E_MAP, reasoning_effort_default=50)
        msgs = [{"role": "user", "content": "hi"}]
        for name, want in E_MAP.items():
            with self.subTest(name=name):
                got = self.sent({"model": "prod", "messages": msgs,
                                 "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": name}})
                self.assertEqual(got["chat_template_kwargs"], {"enable_thinking": True, "reasoning_effort": want})
                self.assertEqual(got["model"], "served-model")
        # the top-level OpenAI field is moved into chat_template_kwargs, so the engine sees one value
        got = self.sent({"model": "prod", "messages": msgs, "reasoning_effort": "high"})
        self.assertEqual(got["chat_template_kwargs"], {"reasoning_effort": 50})
        self.assertNotIn("reasoning_effort", got)
        # a request that names no effort gets the default
        self.assertEqual(self.sent({"model": "prod", "messages": msgs})["chat_template_kwargs"], {"reasoning_effort": 50})

    def test_thinking_off_numbers_and_unmapped_names_go_on_as_sent(self):
        self.start(reasoning_effort_map=E_MAP, reasoning_effort_default=50)
        msgs = [{"role": "user", "content": "hi"}]
        for body in ({"reasoning_effort": "none"},
                     {"chat_template_kwargs": {"enable_thinking": False}},
                     {"chat_template_kwargs": {"thinking": False}},
                     {"chat_template_kwargs": {"reasoning_effort": 80}},
                     {"chat_template_kwargs": {"reasoning_effort": "medium"}}):
            with self.subTest(body=body):
                got = self.sent({"model": "prod", "messages": msgs, **body})
                self.assertEqual({k: v for k, v in got.items() if k not in ("model", "messages")}, body)

    def test_only_chat_requests_get_an_effort(self):
        self.start(reasoning_effort_map=E_MAP, reasoning_effort_default=50)
        for path, body in (("/v1/completions", {"model": "prod", "prompt": "hi"}),
                           ("/v1/embeddings", {"model": "prod", "input": "hi"}),
                           ("/tokenize", {"model": "prod", "prompt": "hi"}),
                           ("/v1/responses", {"model": "prod", "input": "hi", "reasoning": {"effort": "low"}})):
            with self.subTest(path=path):
                self.assertEqual(self.sent(body, path), {**body, "model": "served-model"})

    def test_an_effort_given_elsewhere_is_not_overridden_by_the_default(self):
        self.start(reasoning_effort_map=E_MAP, reasoning_effort_default=50)
        body = {"model": "prod", "messages": [{"role": "user", "content": "hi"}], "reasoning": {"effort": "low"}}
        self.assertEqual(self.sent(body), {**body, "model": "served-model"})

    def test_an_engine_without_a_map_gets_the_effort_it_was_sent(self):
        self.start()
        got = self.sent({"model": "prod", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high"}})
        self.assertEqual(got["chat_template_kwargs"], {"reasoning_effort": "high"})
        self.assertEqual(self.sent({"model": "prod", "messages": []}).get("chat_template_kwargs"), None)

    def test_a_bad_map_is_refused_at_load(self):
        for engine in ({"reasoning_effort_map": {"high": 0}}, {"reasoning_effort_map": {"high": 101}},
                       {"reasoning_effort_map": {}}, {"reasoning_effort_map": ["high"]}, {"reasoning_effort_default": 0}):
            with self.subTest(engine=engine), self.assertRaisesRegex(C.ConfigError, "reasoning_effort_"):
                C.parse({"gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
                         "service": [{"name": "prod", "listen": "127.0.0.1:0", "driver": "vllm",
                                      "upstream": "http://127.0.0.1:1", "gpu_gib": 60, **engine}]})


if __name__ == "__main__":
    unittest.main()
