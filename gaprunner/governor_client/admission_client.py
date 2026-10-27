#!/usr/bin/env python3
"""GapRunner's admission client (the `admission_client` module a governor folder holds).

An app that asks a workload governor before it starts heavy GPU work (Imagination Studio's SPARK_GOVERNOR is one)
imports this file from the folder GapRunner writes, or runs it:

    python3 admission_client.py --owner NAME --workload KIND --socket PATH \
        [--estimated-incremental-gib G] [--min-free-gib F] [--phase start] [--residency new] --run COMMAND ...

--run asks GapRunner for a reservation and waits in GapRunner's queue until it is granted. The command then runs in a
process session of its own with SPARK_ADMISSION_TOKEN and SPARK_WORKLOAD_OWNER set; the reservation is bound to that
session and released when the command ends. When GapRunner needs the GPU back it revokes the reservation: the command
gets SIGTERM, then SIGKILL after the grace period GapRunner gives. Exit status 3 means no reservation was granted.

rpc(request, socket_path) sends one request and returns GapRunner's answer (ops: preflight, status, release).
Standard library only.
"""
import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import process_binding  # noqa: E402

DENIED = 3


def _connect(path, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    return s


def _send(s, obj):
    s.sendall((json.dumps(obj) + "\n").encode())


def _read(f):
    line = f.readline()
    if not line:
        raise ConnectionError("GapRunner closed the connection")
    return json.loads(line)


def rpc(request, socket_path, timeout=30):
    with _connect(socket_path, timeout) as s:
        _send(s, request)
        return _read(s.makefile("r"))


def _say(msg):
    print(f"[gaprunner] {msg}", file=sys.stderr, flush=True)


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(args, command):
    request = {"op": "reserve", "owner": args.owner, "workload": args.workload, "phase": args.phase,
               "estimated_incremental_gib": args.estimated_incremental_gib, "min_free_gib": args.min_free_gib,
               "residency": args.residency, "pid": os.getpid()}
    try:
        s = _connect(args.socket, None)          # a grant may take minutes: GapRunner answers when it has made room
    except OSError as e:
        _say(f"can't reach GapRunner at {args.socket}: {e}")
        return DENIED
    f = s.makefile("r")
    _send(s, request)
    try:
        answer = _read(f)
    except (OSError, ValueError) as e:
        _say(f"no answer from GapRunner: {e}")
        return DENIED
    if not answer.get("allowed"):
        _say("not admitted: " + str(answer.get("reason") or answer))
        return DENIED
    token, grace = answer["token"], float(answer.get("grace_s", 30))
    child = subprocess.Popen(command, env={**os.environ, "SPARK_ADMISSION_TOKEN": token,
                                           "SPARK_WORKLOAD_OWNER": args.owner},
                             start_new_session=True)
    bound = process_binding.binding(child.pid)
    _send(s, {"op": "bind", "token": token, "binding": bound})
    stop = {"at": None, "grace": grace}

    def asked_to_stop(*_):
        if stop["at"] is None:
            stop["at"] = time.monotonic()
            _signal_group(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, asked_to_stop)
    signal.signal(signal.SIGINT, asked_to_stop)
    conn = s
    while child.poll() is None:
        if stop["at"] is not None and time.monotonic() - stop["at"] > stop["grace"]:
            _signal_group(child.pid, signal.SIGKILL)
        if conn is None:
            conn = _rebind(args, token, bound)   # GapRunner restarted: tell the new one this session holds the GPU
            if conn is None:
                time.sleep(1)
                continue
            f = conn.makefile("r")
        try:
            ready, _, _ = select.select([conn], [], [], 0.5)
            if not ready:
                continue
            msg = _read(f)
        except (OSError, ValueError, ConnectionError):
            conn = None
            continue
        if msg.get("op") == "revoke":
            stop["grace"] = float(msg.get("grace_s", grace))
            _say("GapRunner needs the GPU back: stopping " + " ".join(command[:2]))
            asked_to_stop()
    rc = child.returncode
    # the session may outlive its leader (a child that ignored SIGTERM): the reservation stays until all of it ends
    while process_binding.session(bound):
        if stop["at"] is not None and time.monotonic() - stop["at"] > stop["grace"]:
            _signal_group(bound["sid"], signal.SIGKILL)
        time.sleep(0.5)
    try:
        if conn is not None:
            _send(conn, {"op": "release", "token": token})
        else:
            rpc({"op": "release", "owner": args.owner, "token": token}, args.socket)
    except (OSError, ValueError, ConnectionError):
        pass                                     # GapRunner sees the session gone and releases it itself
    return rc if rc >= 0 else 128 - rc


def _rebind(args, token, bound):
    try:
        s = _connect(args.socket, 10)
        _send(s, {"op": "rebind", "owner": args.owner, "token": token, "binding": bound, "workload": args.workload,
                  "estimated_incremental_gib": args.estimated_incremental_gib, "pid": os.getpid()})
        answer = _read(s.makefile("r"))
        if not answer.get("allowed"):
            _say("GapRunner did not take the reservation back: " + str(answer.get("reason")))
        s.settimeout(None)
        return s
    except (OSError, ValueError, ConnectionError):
        return None


def main(argv):
    if "--run" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    cut = argv.index("--run")
    p = argparse.ArgumentParser(prog="admission_client.py")
    p.add_argument("--owner", required=True)
    p.add_argument("--workload", default="")
    p.add_argument("--phase", default="start")
    p.add_argument("--socket", required=True)
    p.add_argument("--estimated-incremental-gib", type=float, default=0.0)
    p.add_argument("--min-free-gib", type=float, default=0.0)
    p.add_argument("--residency", default="")
    args = p.parse_args(argv[:cut])
    command = argv[cut + 1:]
    if not command:
        p.error("--run needs a command")
    return run(args, command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
