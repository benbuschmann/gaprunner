"""Test doubles: a vLLM-like HTTP server and an in-memory driver bound to a fake GPU."""
import http.server
import json
import threading
import time

from gaprunner import drivers

drivers.CACHE_S = 0          # tests need immediate process discovery
from gaprunner.gpu import FakeGpu
from gaprunner.scheduler import HOT, PARKED

GPU = FakeGpu(100.0)


class Backlog(http.server.ThreadingHTTPServer):
    request_queue_size = 128


class FakeVllm:
    """/sleep, /wake_up, /is_sleeping, /v1/chat/completions (streams 3 chunks). Tracks its memory on a FakeGpu."""

    def __init__(self, gpu=None, name="prod", gib=60.0, wake_delay=0.05, down_file=None):
        self.sleeping, self.calls, self.wake_delay, self.down_file = False, [], wake_delay, down_file
        self.gpu, self.name, self.gib = gpu, name, gib
        if gpu is not None:
            gpu.resident[name] = gib
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                d = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

            def down(self):
                import os
                if fake.down_file and os.path.exists(fake.down_file):
                    if fake.gpu is not None:
                        fake.gpu.resident.pop(fake.name, None)
                    self._json(503, {"error": "stopped"})
                    return True
                if fake.gpu is not None and not fake.sleeping:
                    fake.gpu.resident.setdefault(fake.name, fake.gib)
                return False

            def do_GET(self):
                if self.down():
                    return
                if self.path == "/is_sleeping":
                    return self._json(200, {"is_sleeping": fake.sleeping})
                self._json(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if self.down():
                    return
                fake.calls.append(self.path.split("?")[0])
                if self.path.startswith("/sleep"):
                    fake.sleeping = True
                    if fake.gpu is not None:
                        fake.gpu.resident.pop(fake.name, None)
                    return self._json(200, {})
                if self.path == "/wake_up":
                    time.sleep(fake.wake_delay)
                    fake.sleeping = False
                    if fake.gpu is not None:
                        fake.gpu.resident[fake.name] = fake.gib
                    return self._json(200, {})
                if self.path == "/v1/chat/completions":
                    if fake.sleeping:
                        return self._json(500, {"error": "asleep"})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(3):
                        c = f"data: {i}\n\n".encode()
                        self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                self._json(404, {})

        self.srv = Backlog(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class FakeJob(drivers.Driver):
    """A background job on a FakeGpu. spec: gpu (FakeGpu), gpu_gib, stuck (park raises), start (initial state)."""
    registry = {}

    def __init__(self, spec):
        super().__init__(spec)
        self.gpu, self.gib = spec["gpu"], spec["gpu_gib"]
        self.st = spec.get("start", PARKED)
        self.stuck = spec.get("stuck", False)
        self.slow_park_s = float(spec.get("slow_park_s", 0))     # park takes this long; it fails if that's over timeout_s
        self.killed = False
        self.calls = []
        if self.st == HOT:
            self.gpu.resident[spec["name"]] = self.gib
        FakeJob.registry[spec["name"]] = self

    def state(self):
        return self.st

    def park(self):
        self.calls.append("park")
        if self.stuck:
            raise drivers.DriverError("stuck")
        if self.slow_park_s:
            time.sleep(min(self.slow_park_s, self.timeout))
            if self.slow_park_s > self.timeout:
                raise drivers.DriverError(f"did not park within {self.timeout:.1f} s")
        self.st = PARKED
        self.gpu.resident.pop(self.spec["name"], None)

    def resume(self):
        self.calls.append("resume")
        self.st = HOT
        self.gpu.resident[self.spec["name"]] = self.gib

    def kill(self):
        self.killed = True
        self.st = PARKED
        self.gpu.resident.pop(self.spec["name"], None)


drivers.KINDS["fake"] = FakeJob
