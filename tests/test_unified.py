"""Shared memory (DGX Spark / GB10): nvidia-smi reports memory as [N/A], so GapRunner reads the machine's memory, counts
each footprint once against one pool, and measures jobs by the rise in used memory. Discrete GPUs are unchanged."""
import os
import sys
import tempfile
import time
import unittest

from fakes import FakeVllm
from gaprunner import calibrate
from gaprunner import config as C
from gaprunner import drivers
from gaprunner.gpu import FakeGpu, NvidiaSmi
from gaprunner.runner import Runner
from gaprunner.scheduler import decide

PY = sys.executable
SMI = """import os, sys
a = " ".join(sys.argv[1:])
if "--query-gpu=" in a:
    print(os.environ["FAKE_GPU"])
elif "--query-compute-apps" in a:
    print(os.environ.get("FAKE_APPS", ""))
"""


def smi(gpu_row, apps="", **kw):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "smi.py"), "w") as f:
        f.write(SMI)
    with open(os.path.join(d, "meminfo"), "w") as f:
        f.write("MemTotal:       125829120 kB\nMemFree:         1048576 kB\nMemAvailable:   104857600 kB\n")
    os.environ["FAKE_GPU"], os.environ["FAKE_APPS"] = gpu_row, apps
    return NvidiaSmi("0", cmd=(PY, os.path.join(d, "smi.py")), meminfo=os.path.join(d, "meminfo"), **kw)


class ReadingTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FAKE_GPU", None)
        os.environ.pop("FAKE_APPS", None)

    def test_gb10_reads_the_machines_memory(self):
        r = smi("NVIDIA GB10, [N/A], [N/A], 3, 12.5, 41", "4242, python3, [N/A]").read()
        self.assertTrue(r["unified"])
        self.assertEqual((r["total_gib"], r["used_gib"]), (120.0, 20.0))     # MemTotal; MemTotal - MemAvailable
        self.assertEqual(r["procs"], [{"pid": 4242, "gib": 0.0, "gib_known": False, "name": "python3"}])
        self.assertEqual((r["util"], r["power_w"], r["temp_c"]), (3.0, 12.5, 41.0))

    def test_discrete_gpu_is_unchanged(self):
        r = smi("NVIDIA GB300, 256000, 80000, 90, 600, 45", "4242, VLLM::EngineCore, 79872").read()
        self.assertNotIn("unified", r)
        self.assertEqual((r["total_gib"], r["used_gib"]), (250.0, 78.12))
        self.assertEqual(r["procs"], [{"pid": 4242, "gib": 78.0, "name": "VLLM::EngineCore"}])

    def test_a_discrete_gpu_that_cannot_report_memory_is_unreadable(self):
        # never fall back to host memory on a GPU that has its own: that would show it hundreds of GiB it doesn't have
        self.assertIsNone(smi("NVIDIA GB300, [N/A], [N/A], 0, 0, 40").read())

    def test_forced_on_and_off(self):
        self.assertTrue(smi("NVIDIA Thor, [N/A], [N/A], 0, 9, 40", unified=True).read()["unified"])
        self.assertIsNone(smi("NVIDIA GB10, [N/A], [N/A], 0, 9, 40", unified=False).read())

    def test_config_accepts_only_auto_true_false(self):
        base = {"service": [{"name": "prod", "driver": "vllm", "upstream": "http://x", "listen": "127.0.0.1:0", "gpu_gib": 1}]}
        for ok in ("auto", True, False):
            C.parse({**base, "gaprunner": {"unified_memory": ok}})
        with self.assertRaises(C.ConfigError):
            C.parse({**base, "gaprunner": {"unified_memory": "yes"}})


class SchedulingTest(unittest.TestCase):
    def build(self, job_gib, job_cpu=0.0):
        gpu = FakeGpu(128.0, unified=True)
        prod = FakeVllm(gpu, "prod", 60.0)
        cfg = C.parse({"gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp()},
                       "policy": {"reserve_gib": 0, "cpu_floor_gib": 16},
                       "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0",
                                    "gpu_gib": 60}],
                       "job": [{"name": "j", "driver": "restart", "gpu_gib": job_gib, "coexist": "share", "park_s": 0.1,
                                "start_cmd": f"{PY} -c 'import time; time.sleep(30)'", "cpu_gib": job_cpu}]})
        r = Runner(cfg, gpu=gpu)
        r.host_mem = lambda: 5.0                     # would block every start if it were checked separately
        self.addCleanup(lambda: r.job["j"]["driver"].kill())
        r.refresh()
        return r

    def test_one_pool_with_the_floor_taken_off(self):
        s = self.build(50.0, job_cpu=30.0).snapshot()
        self.assertEqual(s.gpu_total_gib, 112.0)                  # 128 - cpu_floor_gib
        self.assertIsNone(s.cpu_free_gib)                         # no second check of the same memory
        self.assertEqual(s.jobs[0].run_cpu_gib, 0.0)

    def test_fits_beside_only_within_the_pool(self):
        r = self.build(50.0)
        self.assertEqual([(a.kind, a.target) for a in decide(r.snapshot(), {}, r.policy)][:1], [("resume_job", "j")])
        r = self.build(60.0)                                      # 60 + 60 > 112
        self.assertNotIn(("resume_job", "j"), [(a.kind, a.target) for a in decide(r.snapshot(), {}, r.policy)])

    def test_owned_processes_are_estimates_not_stowaways(self):
        r = self.build(50.0)
        r.job["j"]["state"] = "hot"
        pid = os.getpid()
        r.job["j"]["driver"].pids = lambda: [pid]
        reading = {"unified": True, "total_gib": 128.0, "used_gib": 120.0,
                   "procs": [{"pid": pid, "gib": 0.0, "gib_known": False, "name": "python"},
                             {"pid": 999999, "gib": 0.0, "gib_known": False, "name": "other"}]}
        gib, measured, stow = r.holders(reading)
        self.assertEqual((gib["j"], measured["j"]), (50.0, False))
        self.assertEqual([p["pid"] for p in stow], [999999])


class ExampleTest(unittest.TestCase):
    def test_spark_example_parses(self):
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = C.load(os.path.join(here, "..", "examples", "dgx-spark.toml"))
        self.assertEqual(cfg["gaprunner"]["unified_memory"], "auto")
        self.assertEqual([j["coexist"] for j in cfg["jobs"]], ["yield", "no"])


class CalibrateTest(unittest.TestCase):
    def test_measures_the_rise_in_used_memory(self):
        ref = [None]

        class SharedGpu:                       # the machine uses 20 GiB; each live job process adds 12
            def read(self, fresh=False):
                r = ref[0]
                n = len(drivers.leaders(r.job["j"]["spec"])) if r is not None else 0
                return {"name": "NVIDIA GB10", "unified": True, "total_gib": 120.0, "used_gib": 20.0 + 12.0 * n,
                        "util": None, "power_w": None, "temp_c": None, "procs": []}

        prod = FakeVllm(None, "prod", 20.0)
        cfg = C.parse({
            "gaprunner": {"mode": "live", "state_dir": tempfile.mkdtemp(), "verify_free_s": 0.5},
            "policy": {"reserve_gib": 0},
            "service": [{"name": "prod", "driver": "vllm", "upstream": prod.url, "listen": "127.0.0.1:0", "gpu_gib": 20}],
            "job": [{"name": "j", "driver": "restart", "gpu_gib": 12, "hold": True, "coexist": "share", "timeout_s": 2,
                     "start_cmd": f"{PY} -c 'import time; time.sleep(60)'"}]})
        r = Runner(cfg, gpu=SharedGpu())
        ref[0] = r
        r.refresh()
        m = calibrate.run(r, "j", warm_s=0.6, cycles=2, settle_s=0.3, poll_s=0.1)
        self.addCleanup(r.job["j"]["driver"].kill)
        self.assertEqual(m["status"], "ok", m["notes"])
        self.assertEqual(m["running"]["hbm_gib"], 12.0)
        self.assertEqual(m["parked"]["hbm_gib"], 0.0)
        self.assertTrue(any("shared memory" in n for n in m["notes"]))


if __name__ == "__main__":
    unittest.main()
