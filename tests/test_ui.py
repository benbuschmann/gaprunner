import http.client
import json
import os
import threading
import time
import unittest

from test_live import build
from gaprunner import ui


class Base(unittest.TestCase):
    """A runner with the web UI on a free port and a PIN of 1234."""
    extra_jobs = ()

    def setUp(self):
        self.r, self.prod, self.gpu, srv, self.stop = build(extra_jobs=self.extra_jobs)
        self.r.top["ui_pin_file"] = os.path.join(self.r.top["state_dir"], "pin")
        open(self.r.top["ui_pin_file"], "w").write("1234\n")
        from gaprunner.gate import Server
        self.srv = Server(("127.0.0.1", 0), ui.make_handler(self.r))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.r.sample()

    def tearDown(self):
        self.stop.set()
        self.srv.shutdown()

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)


class UITest(Base):
    def test_page_and_reads(self):
        code, html = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"GapRunner", html)
        code, st = self.call("GET", "/api/status")
        self.assertEqual(st["queue"][0]["name"], "bench")
        self.assertIn("why", st["jobs"]["bench"])
        self.assertTrue(st["writable"])
        for p in ("/api/history?s=3600", "/api/activity?s=3600", "/api/events?n=10", "/api/summary?s=86400"):
            self.assertEqual(self.call("GET", p)[0], 200, p)

    def test_actions_need_pin_and_take_effect(self):
        self.assertEqual(self.call("POST", "/api/action", {"pin": "nope", "action": "pause"})[0], 403)
        code, out = self.call("POST", "/api/action", {"pin": "1234", "action": "set", "name": "bench", "field": "hold", "value": True})
        self.assertEqual(code, 200, out)
        self.assertTrue(self.call("GET", "/api/status")[1]["jobs"]["bench"]["hold"])
        self.assertEqual(self.call("POST", "/api/action", {"pin": "1234", "action": "window", "hours": 1})[0], 200)
        self.assertTrue(self.call("GET", "/api/status")[1]["window_open"])
        self.assertEqual(self.call("POST", "/api/action", {"pin": "1234", "action": "pause"})[0], 200)
        self.assertTrue(os.path.exists(self.r.top["pause_file"]))
        events = self.call("GET", "/api/events?n=20")[1]
        self.assertEqual(events[0]["event"], "ui_action")

    def test_planned_items(self):
        pin = {"pin": "1234"}
        self.assertEqual(self.call("POST", "/api/action", {**pin, "action": "add", "name": "MiniMax eval", "notes": "window"})[0], 200)
        q = self.call("GET", "/api/status")[1]["queue"]
        self.assertEqual([(x["name"], x["planned"]) for x in q], [("bench", False), ("MiniMax eval", True)])
        self.assertEqual(self.call("POST", "/api/action", {**pin, "action": "move", "name": "MiniMax eval", "dir": "up"})[0], 200)
        self.assertEqual(self.call("GET", "/api/status")[1]["queue"][0]["name"], "MiniMax eval")
        self.assertEqual(self.call("POST", "/api/action", {**pin, "action": "remove", "name": "bench"})[0], 400)
        self.assertEqual(self.call("POST", "/api/action", {**pin, "action": "remove", "name": "MiniMax eval"})[0], 200)
        self.assertEqual(len(self.call("GET", "/api/status")[1]["queue"]), 1)

    def test_read_only_without_pin_file(self):
        os.remove(self.r.top["ui_pin_file"])
        self.assertEqual(self.call("POST", "/api/action", {"pin": "1234", "action": "pause"})[0], 403)


if __name__ == "__main__":
    unittest.main()


class QueueTest(Base):
    extra_jobs = ({"name": "second"},)

    def test_queue_after_another_job(self):
        pin = {"pin": "1234"}
        code, out = self.call("POST", "/api/action", {**pin, "action": "queue", "name": "second", "after": "bench"})
        self.assertEqual(code, 200, out)
        self.assertIn("starts when bench finishes", out["did"])
        time.sleep(0.3)
        st = self.call("GET", "/api/status")[1]
        self.assertEqual(st["jobs"]["second"]["after"], ["bench"])
        self.assertFalse(st["jobs"]["second"]["hold"])
        self.assertEqual(st["jobs"]["second"]["why"], "waiting for bench to finish")
        # a loop is refused
        self.assertEqual(self.call("POST", "/api/action", {**pin, "action": "queue", "name": "bench", "after": "second"})[0], 400)
        # clearing it
        self.assertEqual(self.call("POST", "/api/action", {**pin, "action": "set", "name": "second", "field": "after", "value": ""})[0], 200)
        self.assertEqual(self.call("GET", "/api/status")[1]["jobs"]["second"]["after"], [])


class StartAgainTest(Base):
    def test_start_again_forgets_the_last_exit(self):
        with open(self.r._result_path("bench"), "w") as f:
            f.write('{"rc": 4, "t": 1}')
        self.r.job["bench"]["result"] = {"rc": 4, "t": 1}
        code, out = self.call("POST", "/api/action", {"pin": "1234", "action": "set", "name": "bench", "field": "hold", "value": False})
        self.assertEqual(code, 200, out)
        self.assertIsNone(self.r.job["bench"]["result"])
        self.assertFalse(os.path.exists(self.r._result_path("bench")))


class BulkTest(Base):
    extra_jobs = ({"name": "b2"}, {"name": "b3"})

    def post(self, **body):
        return self.call("POST", "/api/action", {"pin": "1234", **body})

    def names(self):
        return [q["name"] for q in self.call("GET", "/api/status")[1]["queue"]]

    def test_reorder_moves_a_block_before_a_target(self):
        self.assertEqual(self.names(), ["bench", "b2", "b3"])
        code, out = self.post(action="reorder", names=["b3", "b2"], before="bench")
        self.assertEqual(code, 200, out)
        self.assertEqual(self.names(), ["b3", "b2", "bench"])
        self.assertEqual(self.post(action="reorder", names=["b3"])[0], 200)        # no target: to the bottom
        self.assertEqual(self.names(), ["b2", "bench", "b3"])
        self.assertEqual(self.post(action="reorder", names=["nope"])[0], 400)

    def test_chain_queues_in_order(self):
        code, out = self.post(action="chain", names=["b2", "b3"], after="bench")
        self.assertEqual(code, 200, out)
        jobs = self.call("GET", "/api/status")[1]["jobs"]
        self.assertEqual(jobs["b2"]["after"], ["bench"])
        self.assertEqual(jobs["b3"]["after"], ["b2"])
        self.assertFalse(jobs["b3"]["hold"])
        self.assertEqual(self.post(action="chain", names=["bench"], after="b3")[0], 400)   # bench <- b3 <- b2 <- bench

    def test_bulk_clear_start_and_top(self):
        code, out = self.post(action="bulk", op="clear", names=["b2", "b3"])
        self.assertEqual(code, 200, out)
        self.assertIn("Cleared 2 jobs", out["did"])
        q = {x["name"]: x for x in self.call("GET", "/api/status")[1]["queue"]}
        self.assertTrue(q["b2"]["archived"] and q["b2"]["hold"])
        self.post(action="bulk", op="start", names=["b2"])                           # Start brings it back
        self.assertFalse({x["name"]: x for x in self.call("GET", "/api/status")[1]["queue"]}["b2"]["archived"])
        self.post(action="bulk", op="top", names=["b2", "b3"])
        self.assertEqual(self.names(), ["b2", "b3", "bench"])
        code, out = self.post(action="bulk", op="remove", names=["b2"])               # a configured job can't be removed
        self.assertIn("didn't", out["did"])


class TagsAndLogTest(Base):
    def test_tags_override_and_log_endpoint(self):
        code, out = self.call("POST", "/api/action", {"pin": "1234", "action": "set", "name": "bench", "field": "tags", "value": "Nightly, scoring ,nightly"})
        self.assertEqual(code, 200, out)
        st = self.call("GET", "/api/status")[1]
        self.assertEqual(st["jobs"]["bench"]["tags"], ["nightly", "scoring"])
        self.assertEqual(st["queue"][0]["tags"], ["nightly", "scoring"])
        code, log = self.call("GET", "/api/log?name=bench&n=5")
        self.assertEqual(code, 200)
        self.assertIn("lines", log)
        self.assertEqual(self.call("GET", "/api/log?name=nope")[1].get("error"), "no such job")


class RunsTest(Base):
    def test_runs_from_the_audit_log(self):
        t = time.time()
        self.r.audit("start", target="bench", pid=1)
        with open(self.r.audit_path, "a") as f:
            f.write(json.dumps({"t": t + 100, "event": "park", "target": "bench"}) + "\n")
            f.write(json.dumps({"t": t + 200, "event": "resume", "target": "bench"}) + "\n")
            f.write(json.dumps({"t": t + 260, "event": "job_exit", "target": "bench", "rc": 0}) + "\n")
        runs = self.call("GET", "/api/runs")[1]
        last = runs["bench"]["finished"][-1]
        self.assertEqual(last["rc"], 0)
        self.assertAlmostEqual(last["run_s"], 160, delta=2)          # 100 s, stepped aside 100 s, then 60 s



class EmptyPinTest(Base):
    def test_an_empty_pin_file_means_read_only(self):
        open(self.r.top["ui_pin_file"], "w").write("  \n")
        code, out = self.call("POST", "/api/action", {"pin": "", "action": "check"})
        self.assertEqual(code, 403, out)
        self.assertFalse(self.call("GET", "/api/status")[1]["writable"])
