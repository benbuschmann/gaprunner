"""The gate answers a request whose body it can't read with an error at once (bad Content-Length, too large,
chunked), and drops a client that stops sending."""
import socket
import tempfile
import threading
import unittest

from fakes import FakeGpu, FakeVllm
from gaprunner import config as C
from gaprunner import gate
from gaprunner.runner import Runner


class GateRequestBodyTest(unittest.TestCase):
    def setUp(self):
        gpu = FakeGpu(100.0)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 60}],
        })
        self.r = Runner(cfg, gpu=gpu)
        srv = gate.Server(("127.0.0.1", 0), gate.make_handler(self.r, "prod"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.port = srv.server_address[1]

    def status_of(self, headers):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        try:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n" + headers + b"\r\n")   # the body never comes
            line = s.makefile("rb").readline()
        except socket.timeout:
            return None
        finally:
            s.close()
        return int(line.split()[1]) if line else None

    def test_unreadable_bodies_get_an_answer_at_once(self):
        self.assertEqual(self.status_of(b"Content-Length: abc\r\n"), 400)
        self.assertEqual(self.status_of(b"Content-Length: -1\r\n"), 400)
        self.assertEqual(self.status_of(b"Content-Length: 99999999999\r\n"), 413)
        self.assertEqual(self.status_of(b"Transfer-Encoding: chunked\r\n"), 411)
        self.assertEqual(self.r.svc["prod"]["inflight"], 0)       # never counted as a request

    def test_a_client_that_stops_sending_is_dropped(self):
        self.assertEqual(gate.make_handler(self.r, "prod").timeout, 60)


if __name__ == "__main__":
    unittest.main()
