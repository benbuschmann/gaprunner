"""Model servers GapRunner doesn't manage: find OpenAI-style servers listening on this machine and ask what they serve.

Hand-run work often shows up on the GPU as an anonymous process ("root: VLLM::EngineCore"); its server usually listens
on a local port. Every SCAN_S this lists listening TCP ports (from /proc/net/tcp*, no root needed), skips the ones
GapRunner knows (its gate, its UI, every engine and job URL), and asks the rest for GET /v1/models, briefly. A port's
answer is cached for CACHE_S, so a quiet machine is asked about each port rarely.
"""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

SCAN_S = 30.0
CACHE_S = 300.0
LOCAL = {"0100007F", "00000000", "00000000000000000000000000000000", "00000000000000000000000001000000"}


def listening_ports():
    """Ports in LISTEN state on 127.0.0.1, 0.0.0.0 or :: (Linux); [] elsewhere."""
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    if len(parts) > 3 and parts[3] == "0A":
                        addr, port = parts[1].split(":")
                        if addr in LOCAL:
                            ports.add(int(port, 16))
        except OSError:
            pass
    return sorted(p for p in ports if p >= 1024)


def probe(port, timeout=0.5):
    """{"port", "models", "owner", "needs_key"} for an OpenAI-style server on this port, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            d = json.loads(r.read(200_000).decode("utf-8", "replace"))
        data = d.get("data") if isinstance(d, dict) else None
        if not isinstance(data, list):
            return None
        return {"port": port, "models": [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")][:6],
                "owner": next((str(m.get("owned_by")) for m in data if isinstance(m, dict) and m.get("owned_by")), ""),
                "needs_key": False}
    except urllib.error.HTTPError as e:
        return {"port": port, "models": [], "owner": "", "needs_key": True} if e.code == 401 else None
    except (OSError, ValueError):
        return None


def known_ports(runner):
    out = set()
    def add(url):
        try:
            u = urlparse(url if "://" in url else "http://" + url)
            if u.port:
                out.add(u.port)
        except ValueError:
            pass
    for s in runner.svc.values():
        add(s["service"].get("listen", ""))
        for e in s["engines"].values():
            add(e["spec"].get("upstream", "") or "")
            add(e["spec"].get("metrics_url", "") or "")
    for j in runner.job.values():
        add(j["spec"].get("metrics_url", "") or "")
        add(j["spec"].get("url", "") or "")
    add(runner.top.get("ui_listen", "") or "")
    return out


class Finder:
    def __init__(self, scan=listening_ports, ask=probe, clock=time.time):
        self.scan, self.ask, self.clock = scan, ask, clock
        self.cache, self.at, self.found = {}, float("-inf"), []

    def refresh(self, known, skip_models=()):
        """Unmanaged model servers, rescanned at most every SCAN_S; servers listing a known model name are left out
        (a router in front of prod lists prod's model)."""
        now = self.clock()
        if now - self.at < SCAN_S:
            return self.found
        self.at = now
        found = []
        for port in self.scan():
            if port in known:
                continue
            hit = self.cache.get(port)
            if not hit or now - hit[0] > CACHE_S:
                hit = (now, self.ask(port))
                self.cache[port] = hit
            srv = hit[1]
            if srv and not set(srv["models"]) & set(skip_models):
                found.append(srv)
        self.found = found
        return found
