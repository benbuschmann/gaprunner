# Jobs API

Other programs can put jobs into GapRunner's queue: an agent queuing an evaluation, a render farm front end, a
nightly script. They never send a command. You write the recipes they may use, as templates with typed parameters,
and give each program a token that says which templates it may use and how much it may ask for.

A job's `start_cmd` runs as GapRunner's user, so anything that could set it could run anything on the machine. The
API is shaped around that: the command is always yours, and a caller only fills in checked values.

## Templates

A template is a `[[job]]` recipe with a `params` table. A caller's values reach the job's commands only as
environment variables, `GAPRUNNER_PARAM_<NAME>`, never as part of the command text.

```toml
[[template]]
name = "eval"
description = "One benchmark suite against a model already on disk"
driver = "restart"
start_cmd = 'python3 run_eval.py --model "$GAPRUNNER_PARAM_MODEL" --suite "$GAPRUNNER_PARAM_SUITE" --items "$GAPRUNNER_PARAM_ITEMS"'
workdir = "/srv/evals"
gpu_gib = 24                  # what a job from this template takes; a caller may ask for more, never less
max_gpu_gib = 48              # the most a caller may ask for
progress_cmd = "python3 run_eval.py --progress"
goal = "{{suite}} on {{model}}"

[template.params.model]
type = "string"
pattern = "[A-Za-z0-9._/-]+"  # the whole value must match
max_len = 200

[template.params.suite]
type = "choice"
choices = ["v3", "v4.1"]
default = "v3"

[template.params.items]
type = "int"
min = 1
max = 500
default = 50
```

- **Values in commands:** use `"$GAPRUNNER_PARAM_<NAME>"`, in double quotes. A shell never runs what a variable
  holds, so a value like `$(rm -rf ~)` or `x'; rm -rf ~` arrives as plain text. Every command of the job (start,
  progress, saved point, summary, health, park and resume hooks) gets these variables.
- **Don't hand a value to something that runs text as code:** `eval`, `sh -c "… $GAPRUNNER_PARAM_X …"`, `ssh host
  "… $X …"`, `python -c`, `xargs` and the like would run a value. Pass it as an argument or read it from the
  environment in your script instead. This is the one rule a template author has to keep; GapRunner can't check a
  shell command for it.
- **`{{param}}` slots** are only for `goal`, `notes` (shown on the page) and `env` values (for your own variable
  names, e.g. `env = {MODEL_PATH = "/models/{{model}}"}`). A template with a slot in a command is refused.
- **Parameter types:** `string` (`max_len`, default 1000; optional `pattern`), `int` and `float` (`min`, `max`),
  `bool`, `choice` (`choices`). A parameter with a `default` may be left out; one without is required unless it says
  `required = false`. Unknown parameters are refused.
- **What a template may set:** the job fields a recipe uses to run and report (`driver`, `start_cmd`, `workdir`,
  `env`, memory, `coexist`, `window_only`, `hold`, progress, saved-point, health and summary commands, timings,
  `tags`, `color`, ...). Its driver is `restart`, `cuda-checkpoint` or `command`, and it needs a `start_cmd`.
  Names, queue order, `after` and process discovery by name (`pgrep`, `pids_cmd`) are not template fields.

## Clients and tokens

```toml
[[client]]
name = "eval-agent"           # 1 to 24 letters, digits, - or _
token_sha256 = "3c67fb78c912cbbbbf66c0a4dd0e63d0b993c2d2c6b17f5027c4fd1df2c1604c"
templates = ["eval"]          # or ["*"] for every template
max_gpu_gib = 32              # a lower cap than the templates' own (optional)
max_jobs = 5                  # unfinished jobs at once (default 5)
queue_at = "bottom"           # where its jobs enter the queue: "bottom" (default) or "top"
hold = false                  # true: its jobs start wound down, and you press Start on each
```

`gaprunner token` prints a new token and the `token_sha256` line for it. The config never holds the token itself.
Give the token to the program once; a lost token is replaced by making a new one.

The API is served on `ui_listen`, next to the page. Keep that address on loopback or a private network (a VPN such
as Tailscale or WireGuard): GapRunner speaks plain HTTP, so on an open network a token could be read in transit.
Anyone who can reach the address can read `/api/status`, including every job's parameters. Changing templates or
clients takes a GapRunner restart; jobs already submitted are kept.

## Endpoints

Every call needs `Authorization: Bearer <token>`. Errors come back as `{"ok": false, "error": "..."}` with a status
code: 400 (bad values, with every problem listed), 401 (no or unknown token), 403 (over a limit, or a template this
token may not use), 404 (no such template or job), 409 (too many unfinished jobs), 413 (body over 64 KiB).

### `GET /api/templates`

The templates this token may use, with their parameters and the most GPU memory it may ask for.

```json
{"templates": [{"name": "eval", "description": "...", "gpu_gib": 24, "max_gpu_gib": 32,
                "params": {"model": {"type": "string", "pattern": "[A-Za-z0-9._/-]+", "max_len": 200}, ...}}]}
```

### `POST /api/jobs`

```json
{"template": "eval", "params": {"model": "qwen3.8-27b", "suite": "v4.1"}, "name": "nightly v4.1", "gpu_gib": 30}
```

`name` (optional, up to 80 characters) labels the job; GapRunner puts the client's name and an id first, so the job
is called `eval-agent 3f2a91c0: nightly v4.1`. `gpu_gib` (optional) is at least the template's `gpu_gib` and at most
the template's and the client's limits. `hold` (optional) starts it wound down; when the client or the template says
`hold = true`, the caller can't turn that off.

`201` with the job:

```json
{"ok": true, "job": {"id": "3f2a91c0", "name": "eval-agent 3f2a91c0: nightly v4.1", "template": "eval",
                     "params": {"model": "qwen3.8-27b", "suite": "v4.1", "items": 50}, "gpu_gib": 30,
                     "state": "parked", "word": "waiting", "why": "...", "progress": null, "result": null}}
```

From here it is a job like any other: it waits in the queue, runs in gaps or windows by its template's placement,
steps aside for the service, and shows on the page with the tag `via eval-agent`.

### `GET /api/jobs`, `GET /api/jobs/<id>`

This token's jobs, with `state`, `word` and `why` (as on the page), `progress` and `result` (`{"rc": 0}` when done).
A token never sees another client's jobs through these calls; the page and `/api/status` still show every job.

### `DELETE /api/jobs/<id>`

Withdraws the job: it is stopped first if it is running (SIGTERM, then SIGKILL after 15 s), then removed from the
queue.

## What is kept

Submitted jobs are stored in `<state_dir>/submitted.json` with their template and values. At each start GapRunner
makes them again from the current template, so a corrected template applies to jobs already in the queue. A job whose
template is gone, whose values the template no longer accepts, or whose `[[client]]` was removed is not put back (the
audit log says so). Finished jobs
stay until withdrawn, and only unfinished ones count toward `max_jobs`. The audit log records `job_submitted` (with
the client, template and values, long text cut to 200 characters) and `job_withdrawn`.

## Example

```bash
T=grt_...                                    # the client's token
curl -s http://127.0.0.1:8096/api/templates -H "Authorization: Bearer $T"
curl -s -X POST http://127.0.0.1:8096/api/jobs -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"template": "eval", "params": {"model": "qwen3.8-27b", "suite": "v4.1"}}'
curl -s http://127.0.0.1:8096/api/jobs -H "Authorization: Bearer $T"
```

## Apps that start their own server

An app that starts a GPU server itself whenever a person asks for work (a video studio's renderer) is a different
case: it can't be a template, because GapRunner doesn't start it. It asks for a reservation instead
(`driver = "governor"`, see [RECIPES.md](RECIPES.md#apps-that-ask-before-they-start-driver--governor)).
