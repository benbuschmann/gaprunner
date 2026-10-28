"""Idle-gap model: how likely is a service to stay idle a while longer, given how long it has been idle already?

Built from observed idle gaps (seconds between a service finishing its last request and getting the next one).
p_continue(elapsed, need) = P(gap >= elapsed + need | gap >= elapsed), the empirical conditional survival.

Whether "idle longer => likelier to stay idle" holds is a property of each trace, not a law: GapRunner measures it
from your own history (and `gaprunner replay` checks it against simple baselines) rather than assuming it.
"""
import bisect
import collections
import json
import os


class GapModel:
    def __init__(self, gaps=(), max_samples=5000, min_samples=20):
        self.max_samples, self.min_samples = max_samples, min_samples
        self.recent = collections.deque(maxlen=max_samples)
        self.sorted = []
        for g in gaps:
            self.add(g)

    def __len__(self):
        return len(self.sorted)

    def add(self, gap):
        gap = float(gap)
        if gap < 0:
            return
        if len(self.recent) == self.max_samples:
            old = self.recent[0]
            del self.sorted[bisect.bisect_left(self.sorted, old)]
        self.recent.append(gap)
        bisect.insort(self.sorted, gap)

    def _at_least(self, x):
        return len(self.sorted) - bisect.bisect_left(self.sorted, x)

    def p_continue(self, elapsed, need):
        """P(the gap lasts `need` more seconds | it has lasted `elapsed`). None when there is too little history.

        Past the end of the data (fewer than min_samples gaps that long), the estimate is taken at the longest elapsed
        time that still has min_samples behind it, so a very long idle spell is judged like the longest well-observed ones.
        """
        if len(self.sorted) < self.min_samples:
            return None
        if self._at_least(elapsed) < self.min_samples:
            elapsed = self.sorted[-self.min_samples]
        survivors = self._at_least(elapsed)
        return self._at_least(elapsed + need) / survivors

    def median_remaining(self, elapsed):
        """Median further idle time given `elapsed` so far (None without enough history)."""
        if len(self.sorted) < self.min_samples:
            return None
        i = bisect.bisect_left(self.sorted, elapsed)
        rest = self.sorted[i:]
        if len(rest) < self.min_samples:
            rest = self.sorted[-self.min_samples:]
            elapsed = rest[0]
        return rest[len(rest) // 2] - elapsed

    def save(self, path):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"gaps": list(self.recent)}, f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, **kw):
        try:
            with open(path) as f:
                return cls(json.load(f).get("gaps", []), **kw)
        except (FileNotFoundError, ValueError, AttributeError, TypeError):
            return cls(**kw)
