"""Jobs API: other programs put jobs into the queue, from recipes the operator wrote.

A caller never sends a command. The operator writes [[template]] entries: job recipes with typed, limited parameters.
A caller holds a token for one [[client]] entry, which says which templates it may use, how much GPU memory a job may
ask for, how many unfinished jobs it may have and where its jobs enter the queue. A submission names a template and
gives parameter values; GapRunner checks them and adds the job to the queue like any other. Values reach the job's
commands only as environment variables (GAPRUNNER_PARAM_<NAME>, and any env entry with a {{param}} slot), never as
command text: a shell does not run what a variable holds, so no value can become code unless the template's own
command hands it to eval, sh -c and the like. {{param}} slots may also appear in goal and notes (plain text).

Submitted jobs are kept in <state_dir>/submitted.json, so they survive a restart. See docs/JOBS-API.md.

    GET    /api/templates       the templates this token may use, with their parameters
    POST   /api/jobs            {"template", "params", "name"?, "gpu_gib"?, "hold"?}
    GET    /api/jobs            this token's jobs
    GET    /api/jobs/<id>       one of them
    DELETE /api/jobs/<id>       withdraw it (stopped first if it is running)

Every call needs "Authorization: Bearer <token>". The config holds only the token's SHA-256 (`gaprunner token`
makes a pair).
"""
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid

SLOT = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
TYPES = ("string", "int", "float", "bool", "choice")
QUOTED = ("start_cmd", "progress_cmd", "saved_cmd", "summary_cmd", "health_cmd", "park_cmd", "resume_cmd", "state_cmd")
TEXT = ("goal", "notes")
# what a template may set; everything else (names, order, dependencies, process discovery by name) is GapRunner's
JOB_FIELDS = {"driver", "start_cmd", "workdir", "env", "gpu_gib", "running", "parked", "coexist", "yield_resume_s",
              "window_only", "hold", "kind", "goal", "notes", "tags", "color", "progress_cmd", "saved_cmd",
              "summary_cmd", "health_cmd", "health_grace_s", "stall_after_s", "loses_work", "save_every_s", "park_s",
              "resume_s", "timeout_s", "fallback", "max_hours", "metrics_url", "cuda_checkpoint", "park_cmd",
              "resume_cmd", "state_cmd", "ssd_paths", "target_service", "requires_engine"}
TEMPLATE_ONLY = {"name", "description", "params", "max_gpu_gib"}
DRIVERS = ("restart", "cuda-checkpoint", "command")
STATE_FILE = "submitted.json"
MAX_BODY = 65536


class SubmitError(ValueError):
    """A submission GapRunner won't take. code is the HTTP status."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ---------- config ----------

def parse_templates(raw, error):
    out = {}
    for t in raw:
        name = t.get("name")
        if not name or not isinstance(name, str):
            raise error("[[template]] needs a name")
        if name in out:
            raise error(f"two templates are named {name!r}")
        extra = set(t) - JOB_FIELDS - TEMPLATE_ONLY
        if extra:
            raise error(f"template {name}: these fields can't be in a template: {', '.join(sorted(extra))}")
        if t.get("driver") not in DRIVERS:
            raise error(f"template {name}: driver must be one of {', '.join(DRIVERS)}")
        if not t.get("start_cmd"):
            raise error(f"template {name}: start_cmd is required (GapRunner starts submitted jobs)")
        gpu = t.get("gpu_gib", (t.get("running") or {}).get("hbm_gib"))
        if not isinstance(gpu, (int, float)) or gpu <= 0:
            raise error(f"template {name}: gpu_gib (or running.hbm_gib) is required")
        t["max_gpu_gib"] = float(t.get("max_gpu_gib", gpu))
        if t["max_gpu_gib"] < gpu:
            raise error(f"template {name}: max_gpu_gib is below its gpu_gib")
        params = t.setdefault("params", {})
        if not isinstance(params, dict):
            raise error(f"template {name}: params is a table of parameters")
        for pn, p in params.items():
            _check_param(name, pn, p, error)
        used = set()
        for field, value in t.items():
            for text in _strings(value):
                for slot in SLOT.findall(text):
                    if field in QUOTED:
                        raise error(f"template {name}: {field}: commands can't contain {{{{{slot}}}}}; use "
                                    f"\"$GAPRUNNER_PARAM_{slot.upper()}\" (values reach commands only as environment variables)")
                    if field not in TEXT + ("env",):
                        raise error(f"template {name}: {{{{{slot}}}}} can only be used in goal, notes and env")
                    if slot not in params:
                        raise error(f"template {name}: {{{{{slot}}}}} is not one of its params")
                    used.add(slot)
        out[name] = t
    return out


def _check_param(tname, pn, p, error):
    where = f"template {tname} param {pn}"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pn):
        raise error(f"{where}: names are letters, digits and _")
    if not isinstance(p, dict) or p.get("type") not in TYPES:
        raise error(f"{where}: type must be one of {', '.join(TYPES)}")
    if p["type"] == "choice" and not (isinstance(p.get("choices"), list) and p["choices"]):
        raise error(f"{where}: a choice needs choices = [...]")
    if p.get("pattern") is not None:
        try:
            re.compile(p["pattern"])
        except re.error as e:
            raise error(f"{where}: pattern: {e}") from None
    if "default" in p:
        try:
            validate_value(pn, p, p["default"])
        except SubmitError as e:
            raise error(f"{where}: default: {e}") from None


def parse_clients(raw, templates, error):
    out, hashes = {}, set()
    for c in raw:
        name = c.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name):
            raise error("[[client]] needs a name of 1 to 24 letters, digits, - or _")
        if name in out:
            raise error(f"two clients are named {name!r}")
        h = str(c.get("token_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            raise error(f"client {name}: token_sha256 must be the 64-hex SHA-256 of its token (gaprunner token)")
        if h in hashes:
            raise error(f"client {name}: shares a token with another client")
        hashes.add(h)
        allowed = c.get("templates", [])
        if not isinstance(allowed, list) or not allowed:
            raise error(f"client {name}: templates = [...] names what it may use (\"*\" for all)")
        for t in allowed:
            if t != "*" and t not in templates:
                raise error(f"client {name}: template {t!r} is not defined")
        if c.get("queue_at", "bottom") not in ("top", "bottom"):
            raise error(f"client {name}: queue_at is top or bottom")
        if c.get("max_gpu_gib") is not None and not isinstance(c["max_gpu_gib"], (int, float)):
            raise error(f"client {name}: max_gpu_gib is a number")
        out[name] = {"name": name, "token_sha256": h, "templates": allowed, "max_gpu_gib": c.get("max_gpu_gib"),
                     "max_jobs": int(c.get("max_jobs", 5)), "queue_at": c.get("queue_at", "bottom"),
                     "hold": bool(c.get("hold", False))}
    return out


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


# ---------- a submission ----------

def validate_value(pn, p, v):
    t = p["type"]
    if t == "bool":
        if not isinstance(v, bool):
            raise SubmitError(400, f"{pn}: true or false")
        return v
    if t in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "int" and not isinstance(v, int)
                                                                       and not (math.isfinite(v) and v.is_integer())):
            raise SubmitError(400, f"{pn}: {'a whole number' if t == 'int' else 'a number'}")
        v = int(v) if t == "int" else float(v)
        if not math.isfinite(v):
            raise SubmitError(400, f"{pn}: a finite number")
        if p.get("min") is not None and v < p["min"]:
            raise SubmitError(400, f"{pn}: at least {p['min']}")
        if p.get("max") is not None and v > p["max"]:
            raise SubmitError(400, f"{pn}: at most {p['max']}")
        return v
    if t == "choice":
        if v not in p["choices"]:
            raise SubmitError(400, f"{pn}: one of {', '.join(map(str, p['choices']))}")
        return v
    if not isinstance(v, str):
        raise SubmitError(400, f"{pn}: text")
    if "\x00" in v:
        raise SubmitError(400, f"{pn}: no NUL characters")
    if len(v) > int(p.get("max_len", 1000)):
        raise SubmitError(400, f"{pn}: at most {int(p.get('max_len', 1000))} characters")
    if p.get("pattern") is not None and not re.fullmatch(p["pattern"], v):
        raise SubmitError(400, f"{pn}: must match {p['pattern']}")
    return v


def validate_params(template, given):
    if not isinstance(given, dict):
        raise SubmitError(400, "params is an object")
    params = template["params"]
    unknown = set(given) - set(params)
    if unknown:
        raise SubmitError(400, f"unknown params: {', '.join(sorted(unknown))}")
    out, problems = {}, []
    for pn, p in params.items():
        if pn in given:
            try:
                out[pn] = validate_value(pn, p, given[pn])
            except SubmitError as e:
                problems.append(str(e))
        elif "default" in p:
            out[pn] = p["default"]
        elif p.get("required", True):
            problems.append(f"{pn}: required")
    if problems:
        raise SubmitError(400, "; ".join(problems))
    return out


def render(template, values, name, gpu_gib):
    """The job spec for these (checked) values: commands as written; values in the environment, goal and notes."""

    def fill(text):
        return SLOT.sub(lambda m: _text(values[m.group(1)]), text)

    spec = {}
    for k, v in template.items():
        if k in TEMPLATE_ONLY:
            continue
        if k in TEXT and isinstance(v, str):
            spec[k] = fill(v)
        elif k == "env":
            spec[k] = {ek: fill(str(ev)) for ek, ev in v.items()}
        else:
            spec[k] = json.loads(json.dumps(v))
    spec.setdefault("env", {})
    for pn, v in values.items():
        spec["env"][f"GAPRUNNER_PARAM_{pn.upper()}"] = _text(v)
    spec["name"] = name
    spec.pop("gpu_gib", None)
    spec["running"] = {**(spec.get("running") or {}), "hbm_gib": gpu_gib}
    return spec


def _text(v):
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def strict_json(raw):
    """JSON without NaN or Infinity (Python's reader accepts them; browsers and comparisons don't)."""
    def refuse(c):
        raise ValueError(f"{c} is not a number")
    return json.loads(raw, parse_constant=refuse)


def token_pair():
    token = "grt_" + uuid.uuid4().hex + uuid.uuid4().hex
    return token, hashlib.sha256(token.encode()).hexdigest()


# ---------- the API ----------

class Submissions:
    def __init__(self, runner):
        self.runner = runner
        self.templates, self.clients = runner.cfg.get("templates", {}), runner.cfg.get("clients", {})
        self.path = os.path.join(runner.top["state_dir"], STATE_FILE)
        self.rows = []

    def load(self):
        """Put stored submissions back into the queue (at start). One that no longer loads is dropped and logged."""
        try:
            with open(self.path) as f:
                rows = json.load(f).get("jobs", [])
        except (OSError, ValueError):
            rows = []
        for row in rows:
            if row.get("client") not in self.clients:
                self.runner.audit("job_submission_dropped", target=row.get("name"),
                                  error=f"client {row.get('client')!r} is no longer in the config")
                continue
            if row.get("name") in self.runner.job:
                self.rows.append(row)             # already known (a runner built from a config that holds it)
                continue
            try:
                self.runner.add_job(row["spec"])
                self.rows.append(row)
            except Exception as e:  # noqa: BLE001 -- a bad row must not stop GapRunner from starting
                self.runner.audit("job_submission_dropped", target=row.get("name"), error=str(e)[:300])
        if len(self.rows) != len(rows):
            self.save()

    def save(self):
        if self.runner.readonly:
            return
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"jobs": self.rows}, f, indent=1)
        os.replace(tmp, self.path)

    def client_of(self, header):
        """The client a "Bearer <token>" header belongs to, or None."""
        if not header or not header.startswith("Bearer "):
            return None
        h = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        found = None
        for c in self.clients.values():
            if hmac.compare_digest(h, c["token_sha256"]):
                found = c
        return found

    def allowed(self, client):
        return [t for n, t in self.templates.items() if "*" in client["templates"] or n in client["templates"]]

    def describe(self, client):
        return [{"name": t["name"], "description": t.get("description", ""), "gpu_gib": _gpu(t),
                 "max_gpu_gib": min(t["max_gpu_gib"], client["max_gpu_gib"] or math.inf),
                 "params": {pn: {k: v for k, v in p.items()} for pn, p in t["params"].items()}}
                for t in self.allowed(client)]

    def submit(self, client, body):
        if not isinstance(body, dict):
            raise SubmitError(400, "the body is a JSON object")
        unknown = set(body) - {"template", "params", "name", "gpu_gib", "hold"}
        if unknown:
            raise SubmitError(400, f"unknown fields: {', '.join(sorted(unknown))}")
        t = self.templates.get(body.get("template"))
        if t is None:
            raise SubmitError(404, f"no template {body.get('template')!r}")
        if t not in self.allowed(client):
            raise SubmitError(403, f"this token may not use template {t['name']!r}")
        values = validate_params(t, body.get("params", {}))
        gpu = body.get("gpu_gib", _gpu(t))
        if isinstance(gpu, float) and not math.isfinite(gpu):
            raise SubmitError(400, "gpu_gib: a finite number")
        if isinstance(gpu, (int, float)) and not isinstance(gpu, bool) and gpu < _gpu(t):
            raise SubmitError(400, f"gpu_gib: at least {_gpu(t):g} for {t['name']} (what its recipe takes)")
        if isinstance(gpu, bool) or not isinstance(gpu, (int, float)) or not math.isfinite(gpu) or gpu <= 0:
            raise SubmitError(400, "gpu_gib: a positive number")
        cap = min(t["max_gpu_gib"], client["max_gpu_gib"] if client["max_gpu_gib"] is not None else math.inf)
        if gpu > cap:
            raise SubmitError(403, f"gpu_gib {gpu:g} is over this token's limit for {t['name']} ({cap:g})")
        label = str(body.get("name") or t["name"])
        if not re.fullmatch(r"[\w .,:#()+-]{1,80}", label):
            raise SubmitError(400, "name: up to 80 letters, digits, spaces and . , : # ( ) + -")
        hold = body.get("hold", False)
        if not isinstance(hold, bool):
            raise SubmitError(400, "hold: true or false")
        if (client["hold"] or t.get("hold")) and not hold and "hold" in body:
            raise SubmitError(403, "jobs from this token or template start wound down")
        hold = hold or client["hold"] or bool(t.get("hold"))
        with self.runner.lock:
            open_jobs = [r for r in self.rows if r["client"] == client["name"] and not self._finished(r)]
            if len(open_jobs) >= client["max_jobs"]:
                raise SubmitError(409, f"this token already has {len(open_jobs)} unfinished jobs (limit {client['max_jobs']})")
            jid = uuid.uuid4().hex[:8]
            name = f"{client['name']} {jid}: {label}"
            spec = render(t, values, name, float(gpu))
            spec["hold"] = hold
            spec["tags"] = list(spec.get("tags") or []) + [f"via {client['name']}"]
            self.runner.add_job(spec, queue_at=client["queue_at"])
            row = {"id": jid, "name": name, "client": client["name"], "template": t["name"], "params": values,
                   "gpu_gib": float(gpu), "t": round(time.time(), 1), "spec": spec}
            self.rows.append(row)
            self.save()
        self.runner.audit("job_submitted", target=name, client=client["name"], template=t["name"],
                          params={k: (v[:200] if isinstance(v, str) else v) for k, v in values.items()})
        return self.view(row)

    def withdraw(self, client, jid):
        row = self._own(client, jid)
        self.runner.remove_job(row["name"])
        with self.runner.lock:
            self.rows = [r for r in self.rows if r is not row]
            self.save()
        self.runner.audit("job_withdrawn", target=row["name"], client=client["name"])
        return {"id": jid, "withdrawn": True}

    def list(self, client):
        return [self.view(r) for r in self.rows if r["client"] == client["name"]]

    def get(self, client, jid):
        return self.view(self._own(client, jid))

    def _own(self, client, jid):
        row = next((r for r in self.rows if r["id"] == jid), None)
        if row is None or row["client"] != client["name"]:
            raise SubmitError(404, "no such job for this token")
        return row

    def _finished(self, row):
        e = self.runner.job.get(row["name"])
        return e is None or (e["result"] is not None)

    def view(self, row):
        e = self.runner.job.get(row["name"], {})
        st = self.runner.status_of(row["name"]) if row["name"] in self.runner.job else {}
        return {"id": row["id"], "name": row["name"], "template": row["template"], "params": row["params"],
                "gpu_gib": row["gpu_gib"], "submitted_t": row["t"], "state": e.get("state"),
                "word": st.get("word"), "why": st.get("why"), "progress": e.get("progress_text") or None,
                "result": e.get("result")}


def _gpu(t):
    return float(t.get("gpu_gib", (t.get("running") or {}).get("hbm_gib")))
