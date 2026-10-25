"""GPU memory readings from the driver (nvidia-smi). GapRunner trusts these over any engine's word: memory counts as
free only when the driver says so (an idea taken from Berth).

`stowaways()` lists GPU processes that belong to no configured service or job -- reported, never killed.

Shared memory (DGX Spark / GB10): the GPU has no memory of its own, and nvidia-smi reports memory as [N/A]. There the
reading is the machine's memory (MemTotal, and MemTotal - MemAvailable as used), with "unified": True, and per-process
GPU memory is unknown (gib 0.0, "gib_known": False).
"""
import subprocess
import time

MIB_PER_GIB = 1024
UNIFIED_GPUS = ("GB10",)    # GPU names whose memory is the machine's memory


class NvidiaSmi:
    """`index` may be an index, a UUID ("GPU-...") or None: None picks the GPU with the most memory, which on a
    workstation with a small display card is the one you want. Readings are cached for `max_age` seconds."""

    def __init__(self, index=None, cmd=("nvidia-smi",), max_age=0.9, unified="auto", meminfo="/proc/meminfo"):
        self.cmd, self.max_age, self.unified, self.meminfo = list(cmd), max_age, unified, meminfo
        self.index = str(index) if index not in (None, "", "auto") else self._largest()
        self._cache, self._at = None, 0.0

    def _largest(self):
        try:
            r = subprocess.run(self.cmd + ["--query-gpu=uuid,memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=15)
            rows = [line.split(",") for line in r.stdout.strip().splitlines() if line.strip()]
            return max(rows, key=lambda x: float(x[1]))[0].strip()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "0"

    def _q(self, *args):
        r = subprocess.run(self.cmd + ["-i", self.index, *args, "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:200])
        return [[c.strip() for c in line.split(",")] for line in r.stdout.strip().splitlines() if line.strip()]

    def read(self, fresh=False):
        """{"total_gib", "used_gib", "util", "power_w", "temp_c", "name", "procs": [{"pid", "gib", "name"}]},
        or None when the driver can't be read."""
        if not fresh and self._cache and time.monotonic() - self._at < self.max_age:
            return self._cache
        try:
            name, total, used, util, power, temp = self._q(
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,power.draw,temperature.gpu")[0]
            apps = self._q("--query-compute-apps=pid,process_name,used_memory")
        except (RuntimeError, ValueError, IndexError, subprocess.TimeoutExpired, FileNotFoundError):
            return None
        unified = self.unified is True or (self.unified == "auto" and any(u in name for u in UNIFIED_GPUS))
        try:
            if unified:
                mem = _meminfo(self.meminfo)
                total_gib, used_gib = mem["MemTotal"], mem["MemTotal"] - mem["MemAvailable"]
                procs = [{"pid": int(p), "gib": 0.0, "gib_known": False, "name": n} for p, n, _ in apps]
            else:
                total_gib, used_gib = float(total) / MIB_PER_GIB, float(used) / MIB_PER_GIB
                procs = [{"pid": int(p), "gib": round(float(m) / MIB_PER_GIB, 2), "name": n} for p, n, m in apps]
        except (OSError, KeyError, ValueError):
            return None
        self._cache = {"name": name, "total_gib": round(total_gib, 2), "used_gib": round(used_gib, 2),
                       "util": _num(util), "power_w": _num(power), "temp_c": _num(temp), "procs": procs}
        if unified:
            self._cache["unified"] = True
        self._at = time.monotonic()
        return self._cache


def _meminfo(path):
    """{"MemTotal": GiB, "MemAvailable": GiB, ...} from /proc/meminfo."""
    out = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.partition(":")
            if v.strip().endswith("kB"):
                out[k] = int(v.split()[0]) / 1024 / 1024
    return out


def _num(x):
    try:
        return float(x)
    except ValueError:
        return None


class FakeGpu:
    """For tests and replay: memory follows what the test says is resident."""

    def __init__(self, total_gib=100.0, unified=False):
        self.total_gib, self.resident, self.unified = total_gib, {}, unified

    def read(self, fresh=False):
        r = {"name": "fake", "total_gib": self.total_gib, "used_gib": round(sum(self.resident.values()), 2),
             "util": None, "power_w": None, "temp_c": None,
             "procs": [{"pid": 0, "gib": v, "name": k} for k, v in self.resident.items()]}
        if self.unified:                  # like a DGX Spark: memory is the machine's, no per-process figures
            r["unified"], r["procs"] = True, []
        return r


def wait_free(gpu, need_gib, timeout_s, reserve_gib=0.0, poll_s=0.2):
    """Block until the driver shows need_gib free (plus reserve). True on success, False on timeout or no reading."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = gpu.read(fresh=True)
        if r and r["total_gib"] - r["used_gib"] - reserve_gib >= need_gib:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def stowaways(reading, known_pids):
    if not reading:
        return []
    return [p for p in reading["procs"] if p["pid"] and p["pid"] not in known_pids]
