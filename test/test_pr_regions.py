"""Hardware-free checks for PageRank application zones (see docs/gapbs_pr_regions.md).

Uses the shared fake registration library; no NUMA movement, PEBS, or root.
Run from anywhere: python3 gapbs/test/test_pr_regions.py
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

GAPBS = Path(__file__).resolve().parents[1]
ROOT = GAPBS.parent
PR = GAPBS / "pr"
FAKE_SRC = ROOT / "common/regent_regions/tests/fake_regent.cpp"
PAGE = 2 * 1024 * 1024
ZONES = ["incoming_edges", "contributions", "scores", "vertex_index"]


def rows(output, tag):
    return [json.loads(line[len(tag) + 1:]) for line in output.splitlines()
            if line.startswith(tag + " ")]


def top_scores(output):
    return re.findall(r"^(\d+):([\d.e+-]+)$", output, re.M)


class PageRankZonesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pr-regions-test-")
        cls.home = Path(cls.temp.name)
        # Directed input with duplicate edges (0->1), a self loop, an isolated
        # vertex (6), and a sink without outgoing edges (4): exercises
        # compaction and degree handling around the relocated index.
        cls.directed = cls.home / "directed.el"
        cls.directed.write_text("0 1\n0 1\n1 2\n2 0\n3 0\n0 3\n5 4\n7 4\n2 2\n3 5\n")
        cls.fake = cls.home / "fake.so"
        subprocess.run(["g++", "-std=c++11", "-shared", "-fPIC", str(FAKE_SRC), "-o", str(cls.fake)],
                       check=True)
        subprocess.run(["make", "-C", str(GAPBS), "pr"], check=True, capture_output=True)
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("REGENT_", "GAPBS_", "FAKE_")) and k != "LD_PRELOAD"}
        cls.env["OMP_NUM_THREADS"] = "3"

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_pr(self, args, env=None, success=True):
        settings = dict(self.env)
        settings.update(env or {})
        result = subprocess.run([str(PR), *args], env=settings, capture_output=True, text=True, timeout=60)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Average Time:", result.stdout)
            self.assertNotIn('"phase":"kernel_begin"', result.stdout)
        return result

    def application(self, **extra):
        return dict(LD_PRELOAD=str(self.fake), REGENT_REGION_MODE="application",
                    REGENT_FAST_MEMORY="12M", **extra)

    @staticmethod
    def declarations(budgets=("2M", "2M", "4M", "4M")):
        args = ["--app-regions"]
        policies = ["simple_frequency", "evolve", "simple_frequency", "simple_frequency"]
        for zone, policy, budget in zip(ZONES, policies, budgets):
            args += ["--app-region", f"{zone}:{policy}:{budget}"]
        return args

    def test_directed_application_registration_and_results(self):
        legacy = self.run_pr(["-f", str(self.directed), "-n", "2", "-v", "-a"])
        result = self.run_pr(["-f", str(self.directed), "-n", "2", "-v", "-a", "--warmups", "1",
                              *self.declarations()], self.application())
        calls = rows(result.stdout, "FAKE_REGISTER")
        # One range per zone, two for vertex_index on a directed graph, each
        # carrying the whole zone budget.
        self.assertEqual([c["id"] for c in calls], [0, 1, 2, 3, 3])
        self.assertEqual([c["budget"] for c in calls], [PAGE, PAGE, 2 * PAGE, 2 * PAGE, 2 * PAGE])
        self.assertEqual([c["policy"] for c in calls][1], "evolve")
        self.assertTrue(all(c["size"] == PAGE for c in calls))
        manifest, = rows(result.stdout, "REGENT_ZONE_MANIFEST")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual([z["name"] for z in manifest["zones"]], ZONES)
        self.assertEqual([z["runtime_id"] for z in manifest["zones"]], [1, 2, 3, 4])
        bases = [b["base"] for z in manifest["zones"] for b in z["buffers"]]
        self.assertEqual(bases, [c["base"] for c in calls])
        self.assertTrue(manifest["directed"])
        nodes, edges = manifest["nodes"], manifest["edges"]
        self.assertEqual(nodes, 8)
        self.assertEqual(edges, 8)  # duplicate and self edges squished
        zone = {z["name"]: z for z in manifest["zones"]}
        self.assertEqual(zone["incoming_edges"]["logical_bytes"], edges * 4)
        self.assertEqual(zone["scores"]["logical_bytes"], nodes * 4)
        self.assertEqual(zone["contributions"]["logical_bytes"], nodes * 4)
        self.assertEqual([b["buffer"] for b in zone["vertex_index"]["buffers"]], ["in_index", "out_index"])
        self.assertEqual(zone["vertex_index"]["logical_bytes"], 2 * (nodes + 1) * 8)
        self.assertEqual(zone["vertex_index"]["registered_bytes"], 2 * PAGE)
        self.assertEqual(manifest["unmanaged"], [{"name": "out_neighbors", "bytes": edges * 4}])
        self.assertEqual(result.stdout.count("FAKE_RETAINED"), 5)
        # Relocation preserves the graph: same top scores and verification.
        self.assertEqual(top_scores(result.stdout), top_scores(legacy.stdout))
        self.assertEqual(result.stdout.count("Verification:           PASS"), 3)
        trials = rows(result.stdout, "PR_TRIAL")
        self.assertEqual([t["trial"] for t in trials], [-1, 0, 1])
        self.assertTrue(all(t["iterations"] > 0 and t["iterative_seconds"] <= t["seconds"] for t in trials))
        events = rows(result.stdout, "PR_EVENT")
        ready = next(e["monotonic_ns"] for e in events if e["phase"] == "regions_ready")
        starts = [e for e in events if e["phase"] == "kernel_begin"]
        self.assertEqual([e["trial"] for e in starts], [-1, 0, 1])
        self.assertTrue(all(e["monotonic_ns"] >= ready for e in starts))
        self.assertEqual(result.stdout.count("Warmup Time:"), 1)
        measured = [float(x) for x in re.findall(r"^Trial Time:\s+([\d.]+)", result.stdout, re.M)]
        self.assertEqual(len(measured), 2)
        mean = float(re.search(r"Average Time:\s+([\d.]+)", result.stdout)[1])
        self.assertAlmostEqual(mean, sum(measured) / len(measured), places=4)

    def test_undirected_aliases_one_index(self):
        for extra in ([], ["-m"]):
            with self.subTest(in_place=bool(extra)):
                legacy = self.run_pr(["-g", "8", "-n", "1", "-a", *extra])
                result = self.run_pr(["-g", "8", "-n", "1", "-a", "-v", *extra, *self.declarations()],
                                     self.application())
                calls = rows(result.stdout, "FAKE_REGISTER")
                self.assertEqual([c["id"] for c in calls], [0, 1, 2, 3])
                manifest, = rows(result.stdout, "REGENT_ZONE_MANIFEST")
                self.assertFalse(manifest["directed"])
                zone = {z["name"]: z for z in manifest["zones"]}
                self.assertEqual([b["buffer"] for b in zone["vertex_index"]["buffers"]], ["in_index"])
                self.assertEqual(zone["incoming_edges"]["logical_bytes"], 2 * manifest["edges"] * 4)
                self.assertEqual(manifest["unmanaged"], [])
                self.assertEqual(top_scores(result.stdout), top_scores(legacy.stdout))
                self.assertIn("Verification:           PASS", result.stdout)

    def test_directed_in_place_layout(self):
        legacy = self.run_pr(["-f", str(self.directed), "-n", "1", "-a", "-m"])
        layout = self.run_pr(["-f", str(self.directed), "-n", "1", "-a", "-v", "-m", "--region-layout-only"])
        self.assertEqual(top_scores(layout.stdout), top_scores(legacy.stdout))
        manifest, = rows(layout.stdout, "REGENT_ZONE_MANIFEST")
        self.assertEqual(manifest["mode"], "layout")
        self.assertTrue(all(z["registered_bytes"] == 0 for z in manifest["zones"]))
        self.assertTrue(all(z["policy"] is None for z in manifest["zones"]))
        self.assertNotIn("FAKE_REGISTER", layout.stdout)
        self.assertNotIn("REGENT_ZONE_MANIFEST", legacy.stdout)

    def test_failures_prevent_measurement(self):
        base = ["-g", "6", "-n", "1"]
        for args, env in [
            (["--app-regions"], self.application()),
            (self.declarations(), {}),
            (self.declarations(), {"REGENT_REGION_MODE": "application", "REGENT_FAST_MEMORY": "12M"}),
            (self.declarations(), self.application(FAKE_FAIL_ID="0")),
            (self.declarations(), self.application(FAKE_FAIL_ID="3")),
            (self.declarations(), {**self.application(), "REGENT_FAST_MEMORY": "4M"}),
            (self.declarations(), self.application(REGENT_REBALANCER="sweep")),
            ([*self.declarations(), "--region-layout-only"], self.application()),
            (["--region-layout-only"], {"LD_PRELOAD": str(self.fake)}),
            ([*self.declarations(), "--app-region", "scores:simple_frequency:0"], self.application()),
            (self.declarations()[:-2], self.application()),
            (["--app-region", "scores:simple_frequency:0"], {}),
            (["--warmups", "-1"], {}),
            (["--region-layout-only", "-n", "0"], {}),
            ([], {"REGENT_REGION_MODE": "application"}),
        ]:
            with self.subTest(args=args, env=env):
                self.run_pr([*base, *args], env, success=False)
        for bad in ["arms:2M", "simple_frequency:1M", "simple_frequency:2MB", "simple_frequency:x"]:
            with self.subTest(bad=bad):
                args = self.declarations()
                args[2] = "incoming_edges:" + bad
                result = self.run_pr([*base, *args], self.application(), success=False)
                self.assertNotIn("FAKE_REGISTER", result.stdout)

    def test_zero_budgets(self):
        self.run_pr(["-g", "6", "-n", "1", "-v", *self.declarations(("0", "0", "0", "0"))],
                    {**self.application(), "REGENT_FAST_MEMORY": "2M"})

    def test_wrapper_mode_and_quoting(self):
        script = r'''
source "$CUR_PATH/scripts/workloads/gapbs.sh"
config_gapbs
_gapbs_zone_args pr || exit
SYS_ALLOC= HEMEMPOL= DRAMSIZE= MIN_INTERPOSE_MEM_SIZE= REGENT_TARGET_EXE=
PIDFILE="$TEST_HOME/wrapper.pid"
create_workload_wrapper "$TEST_HOME/wrapper.sh" "$PIDFILE" "$TEST_HOME/args.py" "-n 3$ZONE_ARGS" "export OMP_NUM_THREADS=3
$ZONE_EXTRA_ENV"
/bin/sh "$TEST_HOME/wrapper.sh"
'''
        stub = self.home / "args.py"
        stub.write_text("#!/usr/bin/python3\nimport json,os,sys\nprint('ARGS '+json.dumps(sys.argv[1:]))\n"
                        "print('MODE '+str(os.getenv('REGENT_REGION_MODE')))\n"
                        "assert 'REGENT_CLUSTER_CONFIG' not in os.environ\n")
        stub.chmod(0o755)
        env = dict(self.env, CUR_PATH=str(ROOT), TEST_HOME=str(self.home), GAPBS_PR_REGION_MODE="application",
                   GAPBS_PR_INCOMING_EDGES_POLICY="simple_frequency", GAPBS_PR_INCOMING_EDGES_FAST="4M",
                   GAPBS_PR_CONTRIBUTIONS_POLICY="evolve", GAPBS_PR_CONTRIBUTIONS_FAST="2M",
                   GAPBS_PR_SCORES_POLICY="simple_frequency", GAPBS_PR_SCORES_FAST="0",
                   GAPBS_PR_VERTEX_INDEX_POLICY="simple_frequency", GAPBS_PR_VERTEX_INDEX_FAST="2M",
                   GAPBS_PR_WARMUPS="2", REGENT_CLUSTER_CONFIG="stale")
        result = subprocess.run(["bash", "-c", script], env=env, cwd=self.home,
                                capture_output=True, text=True, check=True)
        self.assertIn("MODE application", result.stdout)
        args, = rows(result.stdout, "ARGS")
        self.assertEqual(args, ["-n", "3", "--app-regions",
                                "--app-region", "incoming_edges:simple_frequency:4M",
                                "--app-region", "contributions:evolve:2M",
                                "--app-region", "scores:simple_frequency:0",
                                "--app-region", "vertex_index:simple_frequency:2M",
                                "--warmups", "2"])
        for key in list(env):
            if key.startswith("GAPBS_PR_") and key != "GAPBS_PR_REGION_MODE" or key == "REGENT_CLUSTER_CONFIG":
                env.pop(key)
        env["GAPBS_PR_REGION_MODE"] = "layout"
        result = subprocess.run(["bash", "-c", script], env=env, cwd=self.home,
                                capture_output=True, text=True, check=True)
        self.assertIn("MODE None", result.stdout)
        args, = rows(result.stdout, "ARGS")
        self.assertEqual(args, ["-n", "3", "--region-layout-only"])
        env["GAPBS_PR_REGION_MODE"] = "application"  # missing declarations
        self.assertNotEqual(subprocess.run(["bash", "-c", script], env=env, capture_output=True).returncode, 0)
        env["GAPBS_PR_REGION_MODE"] = "layout"
        self.assertNotEqual(subprocess.run(["bash", "-c", script.replace("_gapbs_zone_args pr", "_gapbs_zone_args bfs")],
                                           env=env, capture_output=True).returncode, 0)

    def test_provenance(self):
        wrapper = self.home / "metadata-wrapper.sh"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        output = self.home / "metadata.json"
        subprocess.run(["python3", str(ROOT / "scripts/workloads/regions_metadata.py"), "--root", str(ROOT),
                        "--binary", str(PR), "--wrapper", str(wrapper), "--output", str(output),
                        "--env-prefix", "GAPBS_", "--glob", "gapbs/src", "*.cc"],
                       env=dict(self.env, GAPBS_PR_REGION_MODE="layout"), check=True)
        data = json.loads(output.read_text())
        self.assertIn(str(PR.resolve()), data["files_sha256"])
        self.assertIn(str(GAPBS / "src/pr.cc"), data["files_sha256"])
        self.assertIn(str(ROOT / "common/regent_regions/zones.h"), data["files_sha256"])
        self.assertIn(str(GAPBS), data["repositories"])
        self.assertEqual(data["environment"]["GAPBS_PR_REGION_MODE"], "layout")


if __name__ == "__main__":
    unittest.main()
