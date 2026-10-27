#!/usr/bin/env python3
"""Keep a GapRunner machine in Imagination Studio's worker list only while GapRunner would admit its renderer.

Run on the head (the machine whose Studio hands clips to workers), e.g. from cron every minute:

    * * * * * python3 wass_worker_sync.py --gaprunner http://station:8096 --job "WASS video" \
                  --worker station-studio --key-cmd "bash ~/imagination-studio/scripts/studio-key.sh --key"

The worker's renderer asks GapRunner before it starts (driver = "governor"). When GapRunner wouldn't admit it now
(the job is window-only and no window is open, it is wound down, the kill switch is on), a clip sent there would only
fail after the Studio's start-up wait. This turns the worker's "use" off at those times and on again when GapRunner
would admit it, so the head sends it clips only when it can render them. It changes nothing while that worker is
rendering, and nothing when GapRunner can't be read.
"""
import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request


def call(method, url, key=None, body=None):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"X-Studio-Key": key} if key else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--gaprunner", required=True, help="GapRunner's page address, e.g. http://station:8096")
    p.add_argument("--job", required=True, help="the governor job's name in GapRunner")
    p.add_argument("--worker", required=True, help="the worker's address in the Studio's Sparks list")
    p.add_argument("--studio", default="http://127.0.0.1:7870", help="the head Studio's address")
    p.add_argument("--key-cmd", required=True, help="a command that prints the head Studio's access key")
    a = p.parse_args()
    try:
        job = call("GET", a.gaprunner.rstrip("/") + "/api/status")["jobs"][a.job]
    except Exception as e:  # noqa: BLE001 -- GapRunner unreadable: leave the worker as it is
        print(f"GapRunner not read ({e}); nothing changed", file=sys.stderr)
        return 1
    want = bool(job.get("admits_now"))
    key = subprocess.run(a.key_cmd, shell=True, capture_output=True, text=True, check=True).stdout.strip()
    sparks = call("GET", a.studio + "/api/sparks", key)
    worker = next((w for w in sparks["workers"] if w["address"] == a.worker), None)
    if worker is None:
        print(f"{a.worker} is not in the Studio's Sparks list", file=sys.stderr)
        return 1
    if worker.get("job"):
        return 0                                  # rendering: never change a worker mid-clip
    if bool(worker.get("enabled")) != want:
        call("POST", f"{a.studio}/api/sparks/workers/{urllib.parse.quote(a.worker, safe='')}", key, {"enabled": want})
        print(f"{a.worker}: use {'on' if want else 'off'}" + ("" if want else f" ({job.get('not_now')})"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
