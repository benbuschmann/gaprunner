"""Park-protocol sidecar: expose any GapRunner driver over Berth's park protocol, so the same job can be parked by
GapRunner *or* by Berth (github.com/T-Crypt/berth, contrib/PARK-PROTOCOL.md).

  gaprunner adapter --driver cuda-checkpoint --pgrep "train.py" --listen 127.0.0.1:19001

  GET  /berth/state   -> 200 {"state": "hot" | "parking" | "parked" | "resuming"}
  POST /berth/park    -> 200 once GPU memory is released; 409 if it could not be
  POST /berth/resume  -> 200 once back on the GPU; 409 if it could not be
Park and resume are idempotent (park when parked, resume when hot: 200, nothing done). One transition at a time.
"""
import http.server
import json
import threading

from .drivers import DriverError
from .scheduler import HOT, PARKED, PARKING, RESUMING


class Adapter:
    def __init__(self, driver):
        self.driver, self.lock, self.transition = driver, threading.Lock(), None

    def state(self):
        return self.transition or self.driver.state()

    def act(self, op):
        with self.lock:
            done, during = (PARKED, PARKING) if op == "park" else (HOT, RESUMING)
            if self.driver.state() == done:
                return 200, {"state": done, "did": "nothing"}
            self.transition = during
            try:
                (self.driver.park if op == "park" else self.driver.resume)()
                return 200, {"state": self.driver.state()}
            except (DriverError, OSError) as e:
                return 409, {"state": self.driver.state(), "error": str(e)[:300]}
            finally:
                self.transition = None


def make_handler(adapter):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/berth/state":
                return self._send(200, {"state": adapter.state()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if self.path in ("/berth/park", "/berth/resume"):
                return self._send(*adapter.act(self.path.rsplit("/", 1)[1]))
            self._send(404, {"error": "not found"})

    return H


def serve(driver, listen):
    host, port = listen.rsplit(":", 1)
    srv = http.server.ThreadingHTTPServer((host, int(port)), make_handler(Adapter(driver)))
    srv.daemon_threads = True
    return srv
