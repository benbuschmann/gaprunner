"""Web UI: one page plus a small JSON API, served by the daemon on `ui_listen`.

Reading needs nothing; changes need the PIN from `ui_pin_file` (no PIN file = read-only UI). The page refreshes
once a minute (and right after a change).

GET  /                          the page
GET  /api/status                live state, why each job is or isn't running, costs, gap model
GET  /api/history?s=3600        GPU memory by holder (5 s rows for the last hour, 1 min rows beyond)
GET  /api/activity?s=3600       service request start/end events
GET  /api/events?n=200          audit log, newest first
GET  /api/summary?s=86400       job GPU-hours, service wakes and added wait over the period
POST /api/action {"pin", "action", ...}
     switch {"service", "engine"} | freeze {"name"} | thaw {"name"} | stop {"name"} | pause | unpause | drain | undrain | clear_gpu_hold | window {"hours": h (0 closes), "exclusive": bool (stop the services)} | move {"name", "dir": "up"|"down"} |
     set {"name", "field": "hold"|"window_only"|"notes", "value"} | add {"name", "notes"} (a planned item) |
     remove {"name"} (planned items only) | calibrate {"name", "warm_s": 60, "cycles": 2} (live mode)

The Jobs API (jobsapi.py, docs/JOBS-API.md) needs "Authorization: Bearer <token>" instead of the PIN:
GET /api/templates | GET /api/jobs | GET /api/jobs/<id> | POST /api/jobs {"template", "params", ...} | DELETE /api/jobs/<id>
"""
import hmac
import http.server
import importlib.resources
import json
import os
import statistics
import time
import urllib.parse

from . import config as C
from .runner import _tail_jsonl


def page():
    return importlib.resources.files("gaprunner").joinpath("ui.html").read_bytes()


def summary(runner, seconds):
    since = runner.clock() - seconds
    rows = runner.history(seconds)
    hours = {}
    for r, nxt in zip(rows, rows[1:]):            # each row covers the time until the next one (at most 2 min)
        dt = min(nxt["t"] - r["t"], 120) / 3600
        for n, st in r.get("st", {}).items():
            if n in runner.job and st == "hot":
                hours[n] = hours.get(n, 0.0) + dt
    ev = _tail_jsonl(runner.audit_path, since)
    wakes = [e for e in ev if e.get("event") == "request_wake"]
    waits = sorted(e.get("total_s", 0) for e in wakes)
    pct = lambda q: round(statistics.quantiles(waits, n=100)[q - 1], 2) if len(waits) > 1 else (waits[0] if waits else None)
    busy = 0.0
    act = runner.activity_since(seconds)
    start = None
    for a in act:
        if a["event"] == "start":
            start = a["t"]
        elif start is not None:
            busy += a["t"] - start
            start = None
    return {"seconds": seconds, "job_gpu_hours": {k: round(v, 2) for k, v in hours.items()},
            "service_wakes": len(wakes), "added_wait_p50": pct(50), "added_wait_p95": pct(95),
            "would": sum(1 for e in ev if e.get("event") == "would"),
            "failures": sum(1 for e in ev if str(e.get("event", "")).endswith("_failed") or e.get("event") == "job_killed"),
            "busy_share": round(busy / seconds, 3) if act else None}


def read_pin(runner):
    """The UI PIN from ui_pin_file, or None when there is none to check against (no file, unreadable, or empty).
    An empty PIN file must never let an empty PIN through, so it means read-only."""
    path = runner.top.get("ui_pin_file")
    try:
        pin = open(path).read().strip() if path else ""
    except OSError:
        return None
    return pin or None


def ordered_queue(runner):
    """The queue as shown: configured jobs and planned items (ideas not yet runnable: no [[job]] entry), in order.
    Configured jobs missing from the queue file are appended in config order."""
    out, seen = [], set()
    for j in C.read_queue(runner.top).get("jobs", []):
        n = j.get("name")
        if not n or n in seen:
            continue
        seen.add(n)
        spec = runner.job.get(n, {}).get("spec", {})
        out.append({"name": n, "planned": n not in runner.job, "coexist": j.get("coexist"),
                    "hold": bool(j.get("hold", spec.get("hold", False))),
                    "window_only": bool(j.get("window_only", spec.get("window_only", False))),
                    "notes": j.get("notes", ""), "added": j.get("added"), "archived": bool(j.get("archived")),
                    **({"tags": j["tags"]} if isinstance(j.get("tags"), list) else {}),
                    **({"after": j["after"]} if isinstance(j.get("after"), list) else {})})
    for n in sorted(runner.job, key=lambda n: runner.job[n]["spec"]["order"]):
        if n not in seen:
            sp = runner.job[n]["spec"]
            out.append({"name": n, "planned": False, "hold": bool(sp["hold"]), "window_only": bool(sp["window_only"]),
                        "notes": "", "added": None, "archived": False})
    return out


def job_log(runner, name, lines=200):
    """The last lines of a job's log (the file GapRunner writes for jobs it starts, or the recipe's log_file)."""
    if name not in runner.job:
        return {"error": "no such job"}
    sp = runner.job[name]["spec"]
    from .runner import _slug
    path = sp.get("log_file") or (os.path.join(runner.top["state_dir"], f"job-{_slug(name)}.log") if sp.get("start_cmd") else None)
    if not path or not os.path.exists(path):
        return {"path": path, "lines": [], "note": "no log yet"}
    lines = max(1, min(int(lines), 2000))
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        tail = f.read().decode("utf-8", "replace").splitlines()[-lines:]
    return {"path": path, "lines": [x[:500] for x in tail], "size": size, "mtime": round(os.path.getmtime(path), 1)}


BULK = {  # bulk op -> the single action it repeats for each selected name
    "start": {"action": "set", "field": "hold", "value": False}, "winddown": {"action": "set", "field": "hold", "value": True},
    "stop": {"action": "stop"}, "pause": {"action": "freeze"}, "resume": {"action": "thaw"},
    "clear": {"action": "archive"}, "unclear": {"action": "unarchive"}, "top": {"action": "move", "dir": "top"},
    "bottom": {"action": "move", "dir": "bottom"}, "remove": {"action": "remove"},
}


def bulk(runner, body):
    """One op over several jobs: each is applied on its own; the reply says what worked and what didn't."""
    op, names = body.get("op"), [str(n) for n in body.get("names", [])]
    if op == "runs":
        v = body.get("value")
        steps = ([{"action": "set", "field": "window_only", "value": True}] if v == "big" else
                 [{"action": "set", "field": "window_only", "value": False}, {"action": "set", "field": "coexist", "value": v}])
    elif op in BULK:
        steps = [BULK[op]]
    else:
        raise ValueError("unknown bulk action")
    if op == "top":
        names = list(reversed(names))              # moving each to the top in reverse keeps the selection's order
    ok, failed = 0, []
    for n in names:
        try:
            for st in steps:
                act(runner, {**st, "name": n})
            ok += 1
        except (ValueError, RuntimeError, KeyError) as e:
            failed.append(f"{n}: {e}")
    word = {"start": "Started", "winddown": "Wound down", "stop": "Stopped", "pause": "Paused", "resume": "Resumed",
            "clear": "Cleared", "unclear": "Brought back", "top": "Moved to the top", "bottom": "Moved to the bottom",
            "remove": "Removed", "runs": "Changed where it runs for"}[op]
    did = f"{word} {ok} job{'s' if ok != 1 else ''}"
    if failed:
        did += f"; {len(failed)} didn't: " + "; ".join(failed[:3]) + ("…" if len(failed) > 3 else "")
    return did


def act(runner, body):
    top, a = runner.top, body.get("action")
    if a == "check":
        return "Unlocked"
    if a == "pause":
        open(top["pause_file"], "w").close()
        sv = " and ".join(runner.svc) or "prod"
        return f"{sv[:1].upper() + sv[1:]} only is on: {sv} stays awake, every job stops, nothing starts"
    if a == "unpause":
        if os.path.exists(top["pause_file"]):
            os.remove(top["pause_file"])
        return "Prod only is off: jobs can run again"
    if a == "window":
        h = float(body.get("hours", 0))
        if h <= 0:
            if os.path.exists(top["window_file"]):
                os.remove(top["window_file"])
            sv = " and ".join(runner.svc) or "prod"
            stopped = any(s["stopped"] or s["stopping"] for s in runner.svc.values())
            return f"No {sv} ended; {sv} is starting again" if stopped else f"Slow-wake {sv} ended"
        ex = bool(body.get("exclusive"))
        if ex and not any(s["spec"].get("stop_cmd") for s in runner.svc.values()):
            raise ValueError("no service has a stop_cmd, so an exclusive window can't stop anything")
        with open(top["window_file"], "w") as f:
            json.dump({"until": time.time() + h * 3600, "exclusive": ex}, f)
        sv = " and ".join(runner.svc) or "prod"
        return (f"No {sv} for {h:g} h: {sv} is stopping so jobs get the whole GPU" if ex
                else f"Slow-wake {sv} for {h:g} h: slow jobs may run; {sv} may take longer to wake")
    if a in ("drain", "undrain"):
        if a == "drain":
            open(runner.drain_path, "w").close()
            return "Winding down all: running jobs stop at their next saved point (or finish their run), nothing new starts"
        if os.path.exists(runner.drain_path):
            os.remove(runner.drain_path)
        return "Wind down ended: jobs can start again"
    if a == "clear_gpu_hold":
        if os.path.exists(runner.gpu_hold_path):
            os.remove(runner.gpu_hold_path)
        return "GPU hold cleared"
    if a == "stop":
        if body.get("name") not in runner.job:
            raise ValueError("no such job")
        return runner.stop_job(body["name"])
    if a in ("freeze", "thaw"):
        if body.get("name") not in runner.job:
            raise ValueError("no such job")
        return runner.freeze(body["name"], a == "freeze")
    if a == "switch":
        return runner.switch_engine(str(body.get("service", "")), str(body.get("engine", "")))
    if a == "calibrate":
        return runner.start_calibration(str(body.get("name", "")), float(body.get("warm_s", 60)), int(body.get("cycles", 2)))
    if a == "bulk":
        return bulk(runner, body)
    q = ordered_queue(runner)
    if a == "reorder":
        # the names in their new order (a dragged block, or the whole list); everything else keeps its relative order
        names = [str(n) for n in body.get("names", [])]
        by = {j["name"]: j for j in q}
        if not names or any(n not in by for n in names) or len(set(names)) != len(names):
            raise ValueError("reorder needs distinct names from the queue")
        before = str(body.get("before") or "")
        rest = [j for j in q if j["name"] not in names]
        k = next((i for i, j in enumerate(rest) if j["name"] == before), len(rest))
        q = rest[:k] + [by[n] for n in names] + rest[k:]
        C.write_queue(top, {"jobs": q})
        return f"Moved {names[0]}" + (f" and {len(names) - 1} more" if len(names) > 1 else "") + (f" before {before}" if before else " to the bottom")
    if a == "chain":
        # Queue selected, in order: each waits for the one before it; the first waits for `after` (or nothing)
        names, dep = [str(n) for n in body.get("names", [])], (body.get("after") or None)
        if len(names) < 1 or len(set(names)) != len(names) or any(n not in runner.job for n in names):
            raise ValueError("chain needs distinct jobs that have recipes")
        if dep is not None and (dep not in runner.job or dep in names):
            raise ValueError(f"{dep} is not another job")
        if dep is not None:
            todo, seen = [dep], set()                # dep must not (eventually) wait for one of these
            while todo:
                cur = todo.pop()
                if cur in names:
                    raise ValueError(f"{dep} already waits for {cur}")
                if cur not in seen:
                    seen.add(cur)
                    todo += runner.after_of(cur)
        by = {j["name"]: j for j in q}
        prev = dep
        for n in names:
            e = by[n]
            e["after"], e["hold"], e["archived"] = ([prev] if prev else []), False, False
            j = runner.job[n]
            j["cap_base"] = j["run_s"]
            if j["result"] is not None:
                runner.clear_result(n)
            prev = n
        C.write_queue(top, {"jobs": q})
        return f"Queued {len(names)} job{'s' if len(names) != 1 else ''} one after another" + (f", starting when {dep} finishes" if dep else "")
    if a == "add":
        name = str(body.get("name", "")).strip()[:120]
        if not name or any(j["name"] == name for j in q):
            raise ValueError("a new, non-empty name is required")
        q.append({"name": name, "planned": name not in runner.job, "hold": False, "window_only": False,
                  "notes": str(body.get("notes", "")).strip()[:500], "added": time.time()})
        C.write_queue(top, {"jobs": q})
        return f"Added the idea {name}"
    i = next((k for k, j in enumerate(q) if j["name"] == body.get("name")), None)
    if i is None:
        raise ValueError("no such job")
    if a == "move":
        d = body.get("dir")
        if d in ("top", "bottom"):
            e = q.pop(i)
            q.insert(0, e) if d == "top" else q.append(e)
            did = f"Moved {body['name']} to the {d}"
        else:
            k = i - 1 if d == "up" else i + 1
            if 0 <= k < len(q):
                q[i], q[k] = q[k], q[i]
            did = f"Moved {body['name']} {d}"
    elif a in ("archive", "unarchive"):
        n = q[i]["name"]
        if a == "archive" and n in runner.job and (runner.job[n]["state"] == "hot" or runner.job[n]["frozen"]):
            raise ValueError(f"{n} is running; stop or wind it down first")
        q[i]["archived"] = a == "archive"
        if a == "archive" and n in runner.job:
            q[i]["hold"] = True                 # a cleared job never starts by itself
        did = f"Cleared {n} (see the Cleared filter)" if a == "archive" else f"Brought back {n}"
    elif a == "set" and body.get("field") in ("hold", "window_only"):
        q[i][body["field"]] = bool(body.get("value"))
        if body["field"] == "hold" and not body.get("value"):
            q[i]["archived"] = False              # Start brings a cleared job back
        if body["field"] == "hold" and not body.get("value") and q[i]["name"] in runner.job:
            j = runner.job[q[i]["name"]]
            j["cap_base"] = j["run_s"]            # a release after max_hours gives it a fresh allowance
            if j["result"] is not None:           # Start again on a done or failed job: forget the last exit
                runner.clear_result(q[i]["name"])
        on = bool(body.get("value"))
        did = ({"hold": {True: f"Wound down {body['name']}", False: f"Started {body['name']}: it runs when there's room"},
                "window_only": {True: f"{body['name']} runs only during slow-wake {' and '.join(runner.svc) or 'prod'}", False: f"{body['name']} can run any time"}}
               [body["field"]][on])
    elif a == "set" and body.get("field") == "coexist":
        if body.get("value") not in ("no", "share", "yield"):
            raise ValueError("coexist is no, share or yield")
        q[i]["coexist"] = body["value"]
        sv = " and ".join(runner.svc) or "prod"
        where = {"no": f"only while {sv} sleeps", "share": f"alongside {sv}", "yield": f"alongside {sv}, giving way while it answers"}
        did = f"{body['name']} runs {where[body['value']]}"
    elif a == "queue" or (a == "set" and body.get("field") == "after"):
        # Queue: start this job once another finishes (exit 0); "" or None = don't wait for a job
        name, dep = body["name"], body.get("after") if a == "queue" else body.get("value")
        dep = dep[0] if isinstance(dep, list) and dep else (dep or None)
        if name not in runner.job:
            raise ValueError("only a job with a recipe can be queued")
        if dep is not None:
            if dep not in runner.job or dep == name:
                raise ValueError(f"{dep} is not another job")
            todo, seen = [dep], set()                # refuse a loop: dep must not (eventually) wait for this job
            while todo:
                cur = todo.pop()
                if cur == name:
                    raise ValueError(f"{dep} already waits for {name}")
                if cur not in seen:
                    seen.add(cur)
                    todo += runner.after_of(cur)
        q[i]["after"] = [dep] if dep else []
        if a == "queue":
            q[i]["hold"] = False
            j = runner.job[name]
            j["cap_base"] = j["run_s"]
            if j["result"] is not None:
                runner.clear_result(name)
            did = f"Queued {name}: it starts when {dep} finishes" if dep else f"Started {name}: it runs when there's room"
        else:
            did = f"{name} starts after {dep} finishes" if dep else f"{name} no longer waits for another job"
    elif a == "set" and body.get("field") == "tags":
        raw = body.get("value", [])
        raw = raw.split(",") if isinstance(raw, str) else raw
        tags = []
        for t in raw:
            t = " ".join(str(t).split())[:24].lower()
            if t and t not in tags:
                tags.append(t)
        q[i]["tags"] = tags[:8]
        did = f"Tags for {body['name']}: {', '.join(tags[:8]) or 'none'}"
    elif a == "set" and body.get("field") == "notes":
        q[i]["notes"] = str(body.get("value", "")).strip()[:500]
        did = f"Updated the notes for {body['name']}"
    elif a == "remove":
        if not q[i]["planned"]:
            raise ValueError("configured jobs are removed from the config file, not the UI")
        del q[i]
        did = f"Removed the idea {body['name']}"
    else:
        raise ValueError("unknown action")
    C.write_queue(top, {"jobs": q})
    return did


def make_handler(runner):
    class UI(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 60                          # a client that stops sending mid-request is dropped

        def _length(self, limit):
            """The request body's length, or an error response's (code, message)."""
            raw = self.headers.get("Content-Length") or "0"
            if not raw.isdigit():
                return None, (400, "Content-Length must be a whole number of bytes")
            n = int(raw)
            if n > limit:
                return None, (413, f"at most {limit} bytes")
            return n, None

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            n = lambda k, d: float(q.get(k, [d])[0])
            path = u.path.rstrip("/") or "/"
            if path == "/":
                return self._send(200, page(), "text/html; charset=utf-8")
            if path == "/api/status":
                st = runner.status()
                st["queue"] = ordered_queue(runner)
                st["writable"] = read_pin(runner) is not None
                return self._send(200, st)
            if path == "/api/history":
                return self._send(200, runner.history(n("s", 3600)))
            if path == "/api/activity":
                return self._send(200, runner.activity_since(n("s", 3600)))
            if path == "/api/events":
                return self._send(200, runner.recent_events(int(n("n", 200))))
            if path == "/api/runs":
                return self._send(200, runner.runs())
            if path == "/api/log":
                return self._send(200, job_log(runner, q.get("name", [""])[0], int(n("n", 200))))
            if path == "/api/summary":
                return self._send(200, summary(runner, n("s", 86400)))
            if path == "/api/templates" or path == "/api/jobs" or path.startswith("/api/jobs/"):
                return self._jobs_api("GET", path)
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path.startswith("/api/jobs/"):
                return self._jobs_api("DELETE", path)
            self._send(404, {"error": "not found"})

        def _jobs_api(self, method, path, body=None):
            from .jobsapi import SubmitError
            subs = runner.submissions
            client = subs.client_of(self.headers.get("Authorization", ""))
            if client is None:
                return self._send(401, {"ok": False, "error": "a valid \"Authorization: Bearer <token>\" is required"})
            jid = path[len("/api/jobs/"):] if path.startswith("/api/jobs/") else None
            try:
                if path == "/api/templates" and method == "GET":
                    return self._send(200, {"templates": subs.describe(client)})
                if path == "/api/jobs" and method == "GET":
                    return self._send(200, {"jobs": subs.list(client)})
                if path == "/api/jobs" and method == "POST":
                    return self._send(201, {"ok": True, "job": subs.submit(client, body)})
                if jid and method == "GET":
                    return self._send(200, {"job": subs.get(client, jid)})
                if jid and method == "DELETE":
                    return self._send(200, {"ok": True, **subs.withdraw(client, jid)})
            except SubmitError as e:
                return self._send(e.code, {"ok": False, "error": str(e)})
            except (ValueError, TypeError, OverflowError, KeyError) as e:
                return self._send(400, {"ok": False, "error": f"bad request: {e}"[:300]})
            self._send(405, {"ok": False, "error": "method not allowed"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path == "/api/jobs":
                from .jobsapi import MAX_BODY
                size, bad = self._length(MAX_BODY)
                if bad:
                    return self._send(bad[0], {"ok": False, "error": bad[1]})
                from .jobsapi import strict_json
                try:
                    body = strict_json(self.rfile.read(size) or b"{}")
                except ValueError as e:
                    return self._send(400, {"ok": False, "error": f"bad json: {e}"})
                return self._jobs_api("POST", path, body)
            if path != "/api/action":
                return self._send(404, {"error": "not found"})
            try:
                size, bad = self._length(8192)
                if bad:
                    return self._send(bad[0], {"ok": False, "error": bad[1]})
                body = json.loads(self.rfile.read(size) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "bad json"})
            pin = read_pin(runner)
            if pin is None:
                return self._send(403, {"ok": False, "error": "this UI is read-only: ui_pin_file is missing or empty"})
            if not hmac.compare_digest(str(body.get("pin", "")).strip().encode(), pin.encode()):
                return self._send(403, {"ok": False, "error": "wrong PIN"})
            try:
                did = act(runner, body)
            except (ValueError, TypeError) as e:
                return self._send(400, {"ok": False, "error": str(e)})
            runner.audit("ui_action", action=body.get("action"), did=did)
            self._send(200, {"ok": True, "did": did})

    return UI


def serve(runner):
    from .gate import Server
    host, port = runner.top["ui_listen"].rsplit(":", 1)
    return Server((host, int(port)), make_handler(runner))
