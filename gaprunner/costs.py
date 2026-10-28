"""Measured switch costs: the last N timings per (target, operation), seeded from config until real samples exist.

Operations: "park" (free the GPU), "resume" (back on the GPU and ready). Seeds come from the config (`park_s`,
`resume_s`); every real transition GapRunner performs is recorded, and the median of the last `keep` samples wins.
"""
import json
import math
import os
import statistics


class CostBook:
    def __init__(self, path=None, keep=20):
        self.path, self.keep = path, keep
        self.samples, self.seeds = {}, {}
        if path:
            try:
                with open(path) as f:
                    samples = json.load(f).get("samples", {})
                if any(not isinstance(v, list) or any(type(x) not in (int, float) or not math.isfinite(x)
                                                      for x in v) for v in samples.values()):
                    raise ValueError("samples must be lists of numbers")
                self.samples = {k: v[-keep:] for k, v in samples.items()}
            except (FileNotFoundError, ValueError, AttributeError, TypeError, OverflowError):
                pass

    @staticmethod
    def _key(name, op):
        return f"{name}:{op}"

    def seed(self, name, op, seconds):
        if seconds is not None:
            self.seeds[self._key(name, op)] = float(seconds)

    def record(self, name, op, seconds):
        k = self._key(name, op)
        self.samples[k] = (self.samples.get(k, []) + [round(float(seconds), 3)])[-self.keep:]
        self._save()

    def get(self, name, op):
        """(seconds, measured?) -- median of recorded samples, else the seed, else (None, False)."""
        s = self.samples.get(self._key(name, op))
        if s:
            return statistics.median(s), True
        return self.seeds.get(self._key(name, op)), False

    def _save(self):
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"samples": self.samples}, f, indent=1)
        os.replace(tmp, self.path)

    def table(self):
        names = sorted({k.rsplit(":", 1)[0] for k in list(self.samples) + list(self.seeds)})
        out = {}
        for n in names:
            row = {}
            for op in ("park", "resume"):
                v, measured = self.get(n, op)
                row[op] = {"seconds": v, "measured": measured, "n": len(self.samples.get(self._key(n, op), []))}
            out[n] = row
        return out
