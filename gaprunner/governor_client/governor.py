"""Machine readings for apps that run under GapRunner's admission (the `governor` module a governor folder holds).

collect_snapshot() returns what the app checks before and while it runs: available memory, the GPU's temperature and
thermal slowdown flags, and memory pressure. A reading that can't be taken is listed in "sensor_errors"; the app treats
that as a reason to stop, never as a pass. Standard library only.

The GPU is the one named by STUDIO_GPU or GAPRUNNER_GPU (an nvidia-smi index); without either, every GPU nvidia-smi
lists.
"""
import os
import subprocess

GIB = 1024 ** 3

_FLAGS = (("hw_thermal", "hw_thermal_slowdown"), ("sw_thermal", "sw_thermal_slowdown"))


def _memory():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            name, _, rest = line.partition(":")
            if rest.strip():
                info[name] = int(rest.split()[0]) * 1024
    return {"available_bytes": info["MemAvailable"], "total_bytes": info["MemTotal"]}


def _psi():
    out = {}
    with open("/proc/pressure/memory") as f:
        for line in f:
            kind, *pairs = line.split()
            values = dict(p.split("=") for p in pairs)
            out[f"{kind}_avg10"] = float(values["avg10"])
            out[f"{kind}_avg60"] = float(values["avg60"])
    return out


def _gpus():
    index = os.environ.get("GAPRUNNER_GPU") or os.environ.get("STUDIO_GPU")
    sel = ["-i", index] if index and index.strip().isdigit() else []
    last = None
    # newer drivers name the fields clocks_event_reasons, older ones clocks_throttle_reasons
    for prefix in ("clocks_event_reasons", "clocks_throttle_reasons"):
        fields = ["index", "temperature.gpu"] + [f"{prefix}.{f}" for _, f in _FLAGS]
        r = subprocess.run(["nvidia-smi", *sel, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            last = (r.stderr or r.stdout).strip()[:200]
            continue
        gpus = []
        for line in r.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            gpus.append({"index": int(parts[0]), "temperature_c": float(parts[1]),
                         "thermal_flags": {name: parts[2 + i].lower() == "active" for i, (name, _) in enumerate(_FLAGS)}})
        return gpus
    raise RuntimeError(f"nvidia-smi: {last}")


def collect_snapshot():
    snap, errors = {}, []
    for key, read in (("memory", _memory), ("psi", _psi), ("gpus", _gpus)):
        try:
            snap[key] = read()
        except Exception as e:  # noqa: BLE001 -- a reading that fails is reported, and the app stops on it
            snap[key] = None
            errors.append(f"{key}: {e}")
    snap["sensor_errors"] = errors
    return snap


def sensor_errors(snapshot):
    return list(snapshot.get("sensor_errors") or [])
