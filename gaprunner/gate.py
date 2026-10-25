"""The gate: one small HTTP proxy per service. Clients talk to the gate instead of the engine; the gate counts
requests (that is how GapRunner sees idle gaps), holds a request while a parked service wakes, then streams the
engine's reply back unchanged (SSE and chunked responses included).

GET /gaprunner/state on any gate returns the daemon's status as JSON.

Requests are sent to the service's current engine (it can be switched), with the JSON "model" field rewritten to the
engine's `model_name` when it has one. A job's own traffic uses the prefix /_job/<job>/ (GapRunner hands jobs that
URL as GAPRUNNER_URL): it keeps the service awake, but isn't counted as the service's activity.

Probe paths (`passive_paths`, default: health, model list, metrics, version, is_sleeping) are passed straight through:
they don't count as activity and never wake a parked service, so health checkers and metrics scrapers don't keep
it awake. A parked vLLM answers them itself.
"""
import http.client
import http.server
import json
import urllib.parse

PASSIVE_DEFAULT = ("/health", "/v1/models", "/metrics", "/version", "/is_sleeping", "/ping")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade"}


class Server(http.server.ThreadingHTTPServer):
    request_queue_size = 128          # the default 5 drops bursts of agent requests
    daemon_threads = True


def make_handler(runner, name):
    svc = runner.svc[name]["service"]
    passive = tuple(svc.get("passive_paths", PASSIVE_DEFAULT))

    class Gate(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self):
            if self.path == "/gaprunner/state":
                return self._send(200, json.dumps(runner.status(), default=str).encode())
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            job, path = None, self.path
            if path.startswith("/_job/"):
                job, _, rest = path[len("/_job/"):].partition("/")
                path = "/" + rest
            probe = urllib.parse.urlparse(path).path.rstrip("/") in passive
            if not probe:
                runner.request_start(name, job)
            sent, conn = False, None
            try:
                if not probe and not runner.ensure_hot(name):
                    why = runner.unavailable(name)
                    msg = why or "service is waking; retry shortly"
                    return self._send(503, json.dumps({"error": {"message": msg, "type": "unavailable"}}).encode(),
                                      extra=[("Retry-After", "60" if why else "5")])
                # pick the engine only now, once the service is ready: a switch that finished meanwhile is honoured
                spec = runner.svc[name]["spec"]
                up = urllib.parse.urlparse(spec["upstream"])
                if body and spec.get("model_name") and (self.headers.get("Content-Type") or "").startswith("application/json"):
                    try:
                        obj = json.loads(body)
                        if isinstance(obj, dict) and "model" in obj:
                            obj["model"] = spec["model_name"]
                            body = json.dumps(obj).encode()
                    except ValueError:
                        pass
                conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=float(svc.get("request_timeout_s", 1800)))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host", "content-length"}}
                headers["Host"] = up.netloc
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                conn.request(self.command, path, body=body, headers=headers)
                r = conn.getresponse()
                chunked = r.getheader("Content-Length") is None
                self.send_response(r.status, r.reason)
                sent = True
                for k, v in r.getheaders():
                    if k.lower() not in HOP and not (chunked and k.lower() == "content-length"):
                        self.send_header(k, v)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" if chunked else chunk)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                conn.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError as e:
                if sent:
                    self.close_connection = True          # mid-stream failure: the client sees a cut stream
                else:
                    try:
                        self._send(502, json.dumps({"error": {"message": f"upstream: {e}", "type": "bad_gateway"}}).encode())
                    except OSError:
                        pass
            finally:
                if conn is not None:
                    conn.close()
                if not probe:
                    runner.request_end(name, job)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy

    return Gate


def serve(runner, name):
    host, port = runner.svc[name]["service"]["listen"].rsplit(":", 1)
    return Server((host, int(port)), make_handler(runner, name))
