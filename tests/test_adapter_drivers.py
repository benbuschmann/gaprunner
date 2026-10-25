import os
import stat
import tempfile
import threading
import unittest

import fakes  # noqa: F401  (registers the fake driver)
from fakes import FakeGpu
from gaprunner import adapter, drivers


class AdapterTest(unittest.TestCase):
    def serve(self, spec):
        srv = adapter.serve(drivers.make(spec), "127.0.0.1:0")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return drivers.make({"name": "via", "driver": "park-protocol", "url": f"http://127.0.0.1:{srv.server_address[1]}"})

    def test_park_protocol_round_trip_and_idempotence(self):
        gpu = FakeGpu(100)
        client = self.serve({"name": "j", "driver": "fake", "gpu": gpu, "gpu_gib": 30, "start": "hot"})
        self.assertEqual(client.state(), "hot")
        client.park()
        self.assertEqual(client.state(), "parked")
        self.assertEqual(gpu.resident, {})
        client.park()                                              # idempotent
        client.resume()
        self.assertEqual(client.state(), "hot")

    def test_failed_park_is_409(self):
        client = self.serve({"name": "s", "driver": "fake", "gpu": FakeGpu(100), "gpu_gib": 30, "start": "hot",
                             "stuck": True})
        with self.assertRaises(drivers.DriverError):
            client.park()


FAKE_CKPT = r'''#!/bin/sh
# fake cuda-checkpoint: state per pid in $CKPT_DIR/<pid>
pid=""; action=""; get=0
while [ $# -gt 0 ]; do case "$1" in --pid) pid=$2; shift;; --action) action=$2; shift;; --get-state) get=1;; --timeout) shift;; esac; shift; done
f="$CKPT_DIR/$pid"; [ -f "$f" ] || echo running > "$f"
s=$(cat "$f")
if [ $get = 1 ]; then echo "$s"; exit 0; fi
case "$action:$s" in
  lock:running) echo locked > "$f";; checkpoint:locked) echo checkpointed > "$f";;
  restore:checkpointed) echo locked > "$f";; unlock:locked) echo running > "$f";;
  *) echo "bad transition $action from $s" >&2; exit 1;;
esac
'''


class CudaCheckpointTest(unittest.TestCase):
    def test_explicit_steps_and_frozen_pids_remembered(self):
        d = tempfile.mkdtemp()
        os.environ["CKPT_DIR"] = d
        b = os.path.join(d, "cuda-checkpoint")
        open(b, "w").write(FAKE_CKPT)
        os.chmod(b, os.stat(b).st_mode | stat.S_IEXEC)
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.kill)
        pidfile = os.path.join(d, "pids")
        open(pidfile, "w").write(f"{proc.pid}\n")
        drv = drivers.make({"name": "train", "driver": "cuda-checkpoint", "pid_file": pidfile, "cuda_checkpoint": b})
        self.assertEqual(drv.state(), "hot")
        drv.park()
        self.assertEqual(drv.state(), "parked")
        os.remove(pidfile)                    # the process is invisible to discovery while checkpointed
        drv.resume()                          # ...but the driver still knows which pid it froze
        self.assertEqual(open(os.path.join(d, str(proc.pid))).read().strip(), "running")


class PidsCmdTest(unittest.TestCase):
    def test_pids_from_a_command(self):
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.kill)
        self.assertEqual(drivers.pids_from({"pids_cmd": f"echo {proc.pid}"}), [proc.pid])


class CommandTest(unittest.TestCase):
    def test_hooks(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "s")
        open(f, "w").write("hot")
        drv = drivers.make({"name": "c", "driver": "command", "park_cmd": f"echo parked > {f}",
                            "resume_cmd": f"echo hot > {f}", "state_cmd": f"cat {f}"})
        drv.park()
        self.assertEqual(drv.state(), "parked")
        drv.resume()
        self.assertEqual(drv.state(), "hot")
        with self.assertRaises(drivers.DriverError):
            drivers.make({"name": "x", "driver": "command", "park_cmd": "exit 3"}).park()


if __name__ == "__main__":
    unittest.main()


class VllmSlowStateTest(unittest.TestCase):
    def test_a_slow_is_sleeping_keeps_the_last_answer(self):
        from fakes import FakeVllm
        from gaprunner import drivers as D
        from gaprunner.scheduler import HOT, UNKNOWN
        fake = FakeVllm(None, "prod", 10.0)
        d = D.Vllm({"name": "prod", "url": fake.url})
        d.STATE_TIMEOUT_S = 0.3
        self.assertEqual(d.state(), HOT)
        fake.state_delay = 1.0                       # busy: the check times out
        self.assertEqual(d.state(), HOT)             # the last real answer stands
        d._last = (HOT, d._last[1] - d.STALE_OK_S - 1)
        self.assertEqual(d.state(), UNKNOWN)         # too old to trust
        fresh = D.Vllm({"name": "prod", "url": fake.url})
        fresh.STATE_TIMEOUT_S = 0.3
        self.assertEqual(fresh.state(), UNKNOWN)     # never had a real answer
