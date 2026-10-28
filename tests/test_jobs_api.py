"""The Jobs API: callers fill in operator-approved templates, limited per token."""
import hashlib
import http.client
import json
import os
import shlex
import threading
import unittest

from test_live import build
from gaprunner import config as C
from gaprunner import jobsapi as J
from gaprunner import ui
from gaprunner.runner import Runner

TOKEN, OTHER, HELD = "grt_test_one", "grt_test_two", "grt_test_held"
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
BASE = {"service": [{"name": "prod", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:0", "gpu_gib": 1}]}
TEMPLATES = [
    {"name": "eval", "description": "a benchmark run", "driver": "restart", "gpu_gib": 20, "max_gpu_gib": 40,
     "start_cmd": "python3 eval.py --model {{model}} --items {{items}} --note {{note}}",
     "goal": "evaluate {{model}}", "env": {"MODEL": "{{model}}"}, "workdir": "/tmp",
     "params": {"model": {"type": "string", "pattern": "[A-Za-z0-9._/-]+", "max_len": 100},
                "items": {"type": "int", "min": 1, "max": 500, "default": 25},
                "note": {"type": "string", "required": False, "default": ""},
                "suite": {"type": "choice", "choices": ["v3", "v4.1"], "default": "v3"}}},
    {"name": "render", "driver": "restart", "gpu_gib": 70, "start_cmd": "render {{prompt}}",
     "params": {"prompt": {"type": "string", "max_len": 2000}}},
]
CLIENTS = [{"name": "agent", "token_sha256": sha(TOKEN), "templates": ["eval"], "max_gpu_gib": 30, "max_jobs": 2},
           {"name": "studio", "token_sha256": sha(OTHER), "templates": ["*"], "queue_at": "top"},
           {"name": "careful", "token_sha256": sha(HELD), "templates": ["eval"], "hold": True}]


class TemplateConfigTest(unittest.TestCase):
    def parse(self, templates=TEMPLATES, clients=CLIENTS):
        return C.parse({**BASE, "template": templates, "client": clients})

    def test_good_config(self):
        cfg = self.parse()
        self.assertEqual(sorted(cfg["templates"]), ["eval", "render"])
        self.assertEqual(cfg["clients"]["studio"]["queue_at"], "top")

    def test_bad_configs(self):
        t = TEMPLATES[0]
        for bad in ({**t, "start_cmd": "x {{nope}}"},                     # a slot with no param
                    {**t, "workdir": "/tmp/{{model}}"},                  # slots only in commands, goal, notes, env
                    {**t, "pgrep": "x"},                                 # not a template field
                    {**t, "driver": "governor"},
                    {**t, "max_gpu_gib": 10},                            # below its own gpu_gib
                    {**t, "params": {"items": {"type": "int", "min": 5, "default": 1}}},
                    {**t, "start_cmd": 'echo "note: {{note}}"'},           # a slot inside double quotes
                    {**t, "start_cmd": "echo 'note: {{note}}'"},           # or single quotes
                    {**t, "progress_cmd": 'sh -c "tail {{note}}"'},
                    {**t, "start_cmd": "python3 - <<EOF\nprint({{note}})\nEOF"},  # a here-document reads it as code
                    {**t, "start_cmd": "bash -c run {{note}}"}, {**t, "start_cmd": "eval run {{note}}"},
                    {**t, "start_cmd": "ssh host run {{note}}"}, {**t, "start_cmd": "echo {{note}} | xargs rm"}):
            with self.assertRaises(C.ConfigError, msg=str(bad)[:80]):
                self.parse([bad, TEMPLATES[1]], [])
        for bad in ({**CLIENTS[0], "token_sha256": "abc"}, {**CLIENTS[0], "templates": ["nope"]},
                    {**CLIENTS[0], "name": "a client"}, {**CLIENTS[0], "name": "x" * 25},
                    {**CLIENTS[0], "queue_at": "middle"}):
            with self.assertRaises(C.ConfigError):
                self.parse(TEMPLATES, [bad])
        with self.assertRaises(C.ConfigError):                           # two clients, one token
            self.parse(TEMPLATES, [CLIENTS[0], {**CLIENTS[1], "token_sha256": sha(TOKEN)}])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.t = C.parse({**BASE, "template": TEMPLATES})["templates"]["eval"]

    def test_values_are_quoted_into_commands(self):
        evil = "x'; rm -rf / #"
        v = J.validate_params(self.t, {"model": "qwen-27b", "note": evil})
        spec = J.render(self.t, v, "eval (agent 1)", 20.0)
        self.assertEqual(shlex.split(spec["start_cmd"]),
                         ["python3", "eval.py", "--model", "qwen-27b", "--items", "25", "--note", evil])
        self.assertEqual(spec["goal"], "evaluate qwen-27b")
        self.assertEqual(spec["env"]["MODEL"], "qwen-27b")
        self.assertEqual(spec["env"]["GAPRUNNER_PARAM_NOTE"], evil)
        self.assertEqual(spec["running"]["hbm_gib"], 20.0)

    def test_slots_as_words_survive_hostile_values(self):
        import subprocess, tempfile
        d = tempfile.mkdtemp()
        t = C.parse({**BASE, "template": [{"name": "x", "driver": "restart", "gpu_gib": 1,
                                           "start_cmd": "printf '%s|' {{a}} --b={{a}} \"$GAPRUNNER_PARAM_A\"",
                                           "params": {"a": {"type": "string"}}}]})["templates"]["x"]
        for evil in ("$(touch %s/pwned)" % d, "`touch %s/pwned`" % d, "a'b\"c; touch %s/pwned" % d, "-rf ~"):
            spec = J.render(t, J.validate_params(t, {"a": evil}), "x", 1.0)
            out = subprocess.run(["sh", "-c", spec["start_cmd"]], capture_output=True, text=True, env=spec["env"]).stdout
            self.assertEqual(out, f"{evil}|--b={evil}|{evil}|")
            self.assertFalse(os.path.exists(f"{d}/pwned"))

    def test_values_are_checked(self):
        for params, needle in (({}, "model: required"), ({"model": "a b"}, "must match"),
                               ({"model": "m", "items": 0}, "at least 1"), ({"model": "m", "items": 2.5}, "whole number"),
                               ({"model": "m", "suite": "v9"}, "one of"), ({"model": "m", "extra": 1}, "unknown params"),
                               ({"model": "m", "items": True}, "whole number"), ({"model": "m" * 101}, "at most 100")):
            with self.assertRaises(J.SubmitError) as e:
                J.validate_params(self.t, params)
            self.assertIn(needle, str(e.exception))


class JobsApiTest(unittest.TestCase):
    def setUp(self):
        self.r, self.prod, self.gpu, srv, self.stop = build()
        self.r.cfg["templates"] = C.parse({**BASE, "template": TEMPLATES})["templates"]
        self.r.cfg["clients"] = J.parse_clients(CLIENTS, self.r.cfg["templates"], C.ConfigError)
        self.r.submissions = J.Submissions(self.r)
        from gaprunner.gate import Server
        self.srv = Server(("127.0.0.1", 0), ui.make_handler(self.r))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.stop.set()
        self.srv.shutdown()

    def call(self, method, path, body=None, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return r.status, data

    def submit(self, token=TOKEN, **body):
        return self.call("POST", "/api/jobs", {"template": "eval", "params": {"model": "qwen"}, **body}, token)

    def test_a_token_is_required(self):
        self.assertEqual(self.call("GET", "/api/jobs", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/jobs", token="grt_wrong")[0], 401)
        self.assertEqual(self.submit(token="grt_wrong")[0], 401)

    def test_templates_list_what_the_token_may_use(self):
        code, out = self.call("GET", "/api/templates")
        self.assertEqual([t["name"] for t in out["templates"]], ["eval"])
        self.assertEqual(out["templates"][0]["max_gpu_gib"], 30)          # the client's limit is lower
        self.assertEqual(sorted(t["name"] for t in self.call("GET", "/api/templates", token=OTHER)[1]["templates"]),
                         ["eval", "render"])

    def test_submit_runs_a_job_from_the_template(self):
        code, out = self.submit(name="nightly eval")
        self.assertEqual(code, 201, out)
        job = out["job"]
        name = job["name"]
        self.assertTrue(name.startswith("agent ") and name.endswith(": nightly eval"))
        self.assertIn(name, self.r.job)
        sp = self.r.job[name]["spec"]
        self.assertIn("--model qwen", sp["start_cmd"])
        self.assertEqual(sp["gpu_gib"], 20.0)
        self.assertIn("via agent", sp["tags"])
        q = self.call("GET", f"/api/jobs/{job['id']}")[1]["job"]
        self.assertEqual(q["template"], "eval")
        self.assertIn(q["word"], ("waiting", "next", "running", "held", "window"))
        st = ui.ordered_queue(self.r)
        self.assertEqual(st[-1]["name"], name)                           # agent's jobs go to the bottom
        events = [json.loads(x)["event"] for x in open(self.r.audit_path)]
        self.assertIn("job_submitted", events)

    def test_limits(self):
        self.assertEqual(self.submit(template="render", params={"prompt": "x"})[0], 403)   # not its template
        self.assertEqual(self.submit(gpu_gib=31)[0], 403)                  # over the client's GPU limit
        self.assertEqual(self.submit(token=OTHER, gpu_gib=41)[0], 403)     # over the template's
        self.assertEqual(self.submit(gpu_gib=30)[0], 201)
        self.assertEqual(self.submit()[0], 201)
        self.assertEqual(self.submit()[0], 409)                            # max_jobs 2
        self.assertEqual(self.submit(template="nope")[0], 404)
        self.assertEqual(self.submit(start_cmd="sh")[0], 400)              # no fields beyond the documented ones
        self.assertEqual(self.submit(name="x; y")[0], 400)

    def test_more_limits(self):
        self.assertEqual(self.submit(gpu_gib=10)[0], 400)                  # less than the recipe takes
        code, out = self.call("POST", "/api/jobs", {"template": ["x"], "params": {}})
        self.assertEqual(code, 400, out)                                   # not a template name
        self.assertEqual(self.submit(params={"model": "m", "items": 10 ** 400})[0], 400)
        long = "a" * 80                                                    # names that would share files
        c1, o1 = self.submit(name=long)
        c2, o2 = self.submit(name=long)
        self.assertEqual((c1, c2), (201, 201))
        from gaprunner.runner import _slug
        self.assertNotEqual(_slug(o1["job"]["name"]), _slug(o2["job"]["name"]))

    def test_bad_lengths(self):
        import socket
        for length in ("-1", "abc"):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(f"POST /api/jobs HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode())
            self.assertIn(b" 400 ", s.recv(200))
            s.close()

    def test_a_template_hold_holds(self):
        tpl = {**TEMPLATES[1], "hold": True}
        self.r.cfg["templates"]["render"] = C.parse({**BASE, "template": [tpl]})["templates"]["render"]
        self.r.submissions.templates = self.r.cfg["templates"]
        self.assertEqual(self.submit(token=OTHER, template="render", params={"prompt": "x"}, hold=False)[0], 403)
        code, out = self.submit(token=OTHER, template="render", params={"prompt": "x"})
        self.assertTrue(self.r.job[out["job"]["name"]]["spec"]["hold"])

    def test_a_removed_client_s_jobs_do_not_come_back(self):
        self.submit()
        self.stop.set()
        fresh = C.parse({**BASE, "gaprunner": {"state_dir": self.r.top["state_dir"]}, "template": TEMPLATES,
                         "client": CLIENTS[1:]})                       # "agent" is gone
        r2 = Runner(fresh, gpu=self.gpu)
        self.assertEqual(r2.submissions.rows, [])
        self.assertFalse(any(n.startswith("agent ") for n in r2.job))

    def test_top_of_the_queue_and_hold(self):
        code, out = self.submit(token=OTHER, template="render", params={"prompt": "a lighthouse"})
        self.assertEqual(code, 201)
        self.assertEqual(ui.ordered_queue(self.r)[0]["name"], out["job"]["name"])
        self.assertEqual(self.submit(token=HELD, hold=False)[0], 403)       # this client's jobs start wound down
        code, out = self.submit(token=HELD)
        self.assertTrue(self.r.job[out["job"]["name"]]["spec"]["hold"])

    def test_own_jobs_only_and_withdraw(self):
        mine = self.submit()[1]["job"]
        theirs = self.submit(token=OTHER)[1]["job"]
        self.assertEqual([j["id"] for j in self.call("GET", "/api/jobs")[1]["jobs"]], [mine["id"]])
        self.assertEqual(self.call("GET", f"/api/jobs/{theirs['id']}")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/jobs/{theirs['id']}")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/jobs/{mine['id']}")[0], 200)
        self.assertNotIn(mine["name"], self.r.job)
        self.assertNotIn(mine["name"], [x["name"] for x in ui.ordered_queue(self.r)])

    def test_submissions_survive_a_restart(self):
        mine = self.submit()[1]["job"]
        self.stop.set()
        fresh = C.parse({**BASE, "gaprunner": {"state_dir": self.r.top["state_dir"]}, "template": TEMPLATES, "client": CLIENTS})
        r2 = Runner(fresh, gpu=self.gpu)                                 # a restart: the config file has no such job
        self.assertIn(mine["name"], r2.job)
        self.assertEqual(r2.submissions.rows[0]["id"], mine["id"])


if __name__ == "__main__":
    unittest.main()
