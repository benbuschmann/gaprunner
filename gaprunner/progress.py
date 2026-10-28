"""Job progress: a recipe's progress_cmd prints how far along the job is; GapRunner turns that into a fraction, a rate
per hour of running time, and an estimate of the GPU-hours still needed.

Line 1 is the whole job; an optional line 2 is the part in progress (e.g. the current training step), shown as a
second bar. Each line: "12/50" (done/total), "24%" or "24.5 %", or a bare number 0..1, plus any words, e.g.
    6/50 steps
    rollouts 18/32
"""
import re
import subprocess

FRACTION = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse(text):
    """Fraction done in [0, 1], or None. The first number in the text wins: "12/50 steps (24%)" -> 12/50,
    "0% overall, stage 1/3" -> 0%."""
    f, pc = FRACTION.search(text), PERCENT.search(text)
    if f and float(f.group(2)) <= 0:
        f = None
    if pc and (not f or pc.start() < f.start()):
        return min(1.0, float(pc.group(1)) / 100)
    if f:
        return min(1.0, float(f.group(1)) / float(f.group(2)))
    try:
        v = float(text.strip().split()[0])
        return v if 0 <= v <= 1 else None
    except (ValueError, IndexError):
        return None


def read(spec, timeout=10):
    """(fraction or None, raw text) for the whole job from the recipe's progress_cmd, run in its workdir."""
    frac, text, _, _ = read_both(spec, timeout)
    return frac, text


def saved_point(spec, timeout=10):
    """The first line saved_cmd prints (the job's last saved point, e.g. a step number), or None."""
    import os
    try:
        r = subprocess.run(["sh", "-c", spec["saved_cmd"]], capture_output=True, text=True, timeout=timeout,
                           cwd=os.path.expanduser(spec.get("workdir", ".")), env={**os.environ, **spec.get("env", {})})
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = r.stdout.strip().splitlines()[:1]
    return line[0].strip()[:80] if r.returncode == 0 and line and line[0].strip() else None


def summary(spec, timeout=20, lines=12):
    """What summary_cmd prints (a few lines a person should see first, e.g. the best results so far), or None."""
    import os
    try:
        r = subprocess.run(["sh", "-c", spec["summary_cmd"]], capture_output=True, text=True, timeout=timeout,
                           cwd=os.path.expanduser(spec.get("workdir", ".")), env={**os.environ, **spec.get("env", {})})
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = [x.rstrip()[:200] for x in r.stdout.strip().splitlines() if x.strip()]
    return out[:lines] if r.returncode == 0 and out else None


def read_both(spec, timeout=10):
    """(fraction, text, part fraction, part text): line 1 is the whole job, line 2 (optional) the part in progress."""
    import os
    cmd = spec.get("progress_cmd")
    if not cmd:
        return None, "", None, ""
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout,
                           cwd=os.path.expanduser(spec.get("workdir", ".")), env={**os.environ, **spec.get("env", {})})
    except (OSError, subprocess.TimeoutExpired):
        return None, "", None, ""
    lines = [x.strip() for x in r.stdout.strip().splitlines() if x.strip()]
    if not lines:
        return None, "", None, ""
    sub = lines[1] if len(lines) > 1 else ""
    return parse(lines[0]), lines[0][:80], (parse(sub) if sub else None), sub[:80]


def rate(samples):
    """Fraction per hour of running time from [(run_seconds, fraction), ...]; None until there is movement."""
    if len(samples) < 2:
        return None
    (s0, f0), (s1, f1) = samples[0], samples[-1]
    if s1 - s0 < 30 or f1 <= f0:
        return None
    return (f1 - f0) / ((s1 - s0) / 3600)


def hours_left(frac, per_hour):
    if frac is None or not per_hour:
        return None
    return round((1 - frac) / per_hour, 2)
