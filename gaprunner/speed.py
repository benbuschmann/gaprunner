"""Engine stats: read an inference server's Prometheus metrics and turn them into the numbers a person checks.

A job or engine gets stats when it has `metrics_url` (a Prometheus endpoint, e.g. http://127.0.0.1:18106/metrics).
Services with the vllm or sglang driver default to <upstream>/metrics. The counters are the server's own totals, so
the numbers cover everything the server did, whoever asked: for prod that includes job traffic through the gate.

Per server, every EVERY_S while it runs:
  tps    {"out", "in", "t", "window_s"}: generated tokens/s and freshly computed prompt tokens/s over ~WINDOW_S.
         "in" leaves out prompt tokens served from the prefix cache, so cached requests don't show as fast prefill.
  tps_last  the last non-zero tps, with its time, so an idle server shows what it last did instead of a zero.
  stats  gauges now (requests running and waiting, KV cache use) and, over up to LATENCY_S, the mean time to first
         token, the mean whole-request time, the prefix-cache hit rate, the cached share of prompt tokens and the
         speculative-decoding acceptance rate. Missing metrics stay None; nothing is made up.
"""
import urllib.request

# one name per engine family for each value; counters are summed over label sets
DIALECT = {
    "out": ("vllm:generation_tokens_total", "sglang:generation_tokens_total"),
    "in": ("vllm:prompt_tokens_total", "sglang:prompt_tokens_total"),
    "in_cached": ("vllm:prompt_tokens_cached_total", "sglang:cached_tokens_total"),
    "prefix_hits": ("vllm:prefix_cache_hits_total",),
    "prefix_queries": ("vllm:prefix_cache_queries_total",),
    "ttft_sum": ("vllm:time_to_first_token_seconds_sum", "sglang:time_to_first_token_seconds_sum"),
    "ttft_n": ("vllm:time_to_first_token_seconds_count", "sglang:time_to_first_token_seconds_count"),
    "e2e_sum": ("vllm:e2e_request_latency_seconds_sum", "sglang:e2e_request_latency_seconds_sum"),
    "e2e_n": ("vllm:e2e_request_latency_seconds_count", "sglang:e2e_request_latency_seconds_count"),
    "spec_acc": ("vllm:spec_decode_num_accepted_tokens_total",),
    "spec_draft": ("vllm:spec_decode_num_draft_tokens_total",),
}
GAUGES = {
    "running": ("vllm:num_requests_running", "sglang:num_running_reqs"),
    "waiting": ("vllm:num_requests_waiting", "sglang:num_queue_reqs"),
    "kv": ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc", "sglang:token_usage"),
    "cache_hit_rate": ("sglang:cache_hit_rate",),
    "spec_len": ("sglang:spec_accept_length",),
}
EVERY_S = 10.0       # how often to read the metrics
WINDOW_S = 30.0      # tokens/s is the average over about this long
LATENCY_S = 300.0    # latency and hit rates over up to this long (requests can be sparse)
NAMES = {n: k for k, ns in DIALECT.items() for n in ns}
GNAMES = {n: k for k, ns in GAUGES.items() for n in ns}


def parse(text):
    """{key: value} for the DIALECT counters (summed over label sets) and GAUGES; None if no known metric is there."""
    tot = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        key = NAMES.get(name) or GNAMES.get(name)
        if not key:
            continue
        try:
            v = float(line.rsplit(" ", 1)[1])
        except ValueError:
            continue
        if key in GAUGES and key in tot and name in GNAMES and key == "kv":
            tot[key] = max(tot[key], v)              # one KV figure: the fullest engine
        else:
            tot[key] = tot.get(key, 0.0) + v
    return tot if ("out" in tot or "in" in tot) else None


def read(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return parse(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def url_of(spec):
    if spec.get("metrics_url"):
        return spec["metrics_url"]
    if spec.get("driver") in ("vllm", "sglang") and spec.get("upstream"):
        return spec["upstream"].rstrip("/") + "/metrics"
    return None


def _delta(new, old, k):
    a, b = new.get(k), old.get(k)
    return None if a is None or b is None else a - b


def update(entry, url, now, running, reader=read):
    """Read the metrics at most every EVERY_S while running; set entry["tps"], entry["tps_last"], entry["stats"]."""
    if not url or not running:
        entry["tps"], entry["stats"], entry["tps_samples"] = None, None, []
        return
    if entry.get("tps_url") not in (None, url):       # another engine (or server): start over
        entry["tps"], entry["stats"], entry["tps_samples"], entry["tps_at"] = None, None, [], float("-inf")
    entry["tps_url"] = url
    if now - entry.get("tps_at", float("-inf")) < EVERY_S:
        return
    entry["tps_at"] = now
    tot = reader(url)
    if tot is None:
        return
    samples = [s for s in entry.get("tps_samples", []) if now - s[0] <= LATENCY_S + EVERY_S]
    if samples and any(k in tot and k in samples[-1][1] and tot[k] < samples[-1][1][k] for k in DIALECT):
        samples, entry["tps"] = [], None               # the server restarted: its counters went back to zero
    samples.append((now, tot))
    entry["tps_samples"] = samples
    gauges = {k: tot.get(k) for k in GAUGES}
    old = next((s for s in samples if now - s[0] <= WINDOW_S + EVERY_S), None)
    if old is not None and now - old[0] >= EVERY_S / 2:
        dt, o = now - old[0], old[1]
        d_out, d_in, d_cached = _delta(tot, o, "out"), _delta(tot, o, "in"), _delta(tot, o, "in_cached")
        fresh_in = None if d_in is None else max(0.0, d_in - (d_cached or 0.0))
        entry["tps"] = {"out": None if d_out is None else round(d_out / dt, 1),
                        "in": None if fresh_in is None else round(fresh_in / dt, 1), "t": round(now, 1), "window_s": round(dt)}
        if (entry["tps"]["out"] or 0) > 0 or (entry["tps"]["in"] or 0) > 0:
            entry["tps_last"] = dict(entry["tps"])
    first = samples[0]
    span, f = now - first[0], first[1]
    ratio = lambda a, b: (lambda x, y: None if x is None or not y else round(x / y, 3))(_delta(tot, f, a), _delta(tot, f, b))
    entry["stats"] = {
        "running": gauges["running"], "waiting": gauges["waiting"],
        "kv": None if gauges["kv"] is None else round(gauges["kv"], 3),
        "ttft_s": ratio("ttft_sum", "ttft_n"), "e2e_s": ratio("e2e_sum", "e2e_n"),
        "requests": _delta(tot, f, "e2e_n"),
        "prefix_hit": ratio("prefix_hits", "prefix_queries") if "prefix_hits" in tot else gauges["cache_hit_rate"],
        "cached_share": ratio("in_cached", "in"),
        "spec_accept": ratio("spec_acc", "spec_draft"), "spec_len": gauges["spec_len"],
        "window_s": round(span), "t": round(now, 1)}
