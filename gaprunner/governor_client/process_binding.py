"""Process identity for GapRunner's admission client (the `process_binding` module a governor folder holds).

A reservation is bound to the process session its command runs in. GapRunner and the app read the same facts from
/proc: the session id, the leader's start time (so a reused pid is never mistaken for the old one) and the owner's uid.
Standard library only; this file is copied into the governor folder and imported by other programs.
"""
import os


def _stat(pid):
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except (OSError, ValueError, IndexError, TypeError):
        return None
    # after the command name: state ppid pgrp session tty tpgid flags ... starttime is the 20th
    return {"state": fields[0], "pgid": int(fields[2]), "sid": int(fields[3]), "start": int(fields[19])}


def _uid(pid):
    try:
        with open(f"/proc/{int(pid)}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, TypeError):
        pass
    return None


def process(pid):
    """{"pid", "start", "uid", "state", "sid", "pgid"} for a process, or None when there is none."""
    st = _stat(pid)
    if st is None:
        return None
    return {"pid": int(pid), "start": st["start"], "uid": _uid(pid), "state": st["state"], "sid": st["sid"],
            "pgid": st["pgid"]}


def binding(pid):
    """The binding for a command started in a session of its own (pid is its leader)."""
    p = process(pid)
    if p is None:
        return None
    return {"sid": p["sid"], "leader": p["pid"], "start": p["start"], "uid": p["uid"]}


def session(bound):
    """Live pids of a bound session (empty when it has ended). Members are matched by session id and owner; the
    kernel does not hand out a pid that is still in use as a session id, so the id stays this session's while any
    member lives."""
    if not bound or not bound.get("sid"):
        return []
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        st = _stat(name)
        if st is None or st["sid"] != bound["sid"] or st["state"] == "Z":
            continue
        if bound.get("uid") is not None and _uid(name) != bound["uid"]:
            continue
        out.append(int(name))
    return sorted(out)
