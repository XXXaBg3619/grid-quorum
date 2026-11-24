"""The skeleton holds together: modules import, four architectures, honest fault defaults,
experiment output helpers."""
import contextlib
import csv
import importlib
import io
import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from experiments import common  # noqa: E402
from gridq import arch, faults  # noqa: E402
from gridq.verifier import REJECT_REASONS  # noqa: E402


class TestSkeleton(unittest.TestCase):
    def test_all_modules_import(self):
        for m in ("gridq", "gridq.sim", "gridq.power", "gridq.loads", "gridq.policy", "gridq.verifier",
                  "gridq.lasthop", "gridq.run", "gridq.consensus", "gridq.arch", "gridq.faults", "gridq.svgchart"):
            importlib.import_module(m)

    def test_four_architectures(self):
        self.assertEqual(sorted(arch.REGISTRY), ["A", "B", "C", "D"])
        for name, cls in arch.REGISTRY.items():
            self.assertEqual(cls.name, name)
            self.assertTrue(issubclass(cls, arch.Architecture))
            self.assertTrue(cls.decision_path)

    def test_honest_fault_defaults(self):
        f = faults.build("honest")
        f.begin_run(None, random.Random(0))
        self.assertFalse(f.active)
        rec = ["v1", "v2"]
        self.assertEqual(f.proposal(None, "v0", 1, 0, "blk", rec), {"v1": "blk", "v2": "blk"})
        self.assertEqual(f.vote(None, "v0", "prevote", 1, 0, "blk", rec), {"v1": "blk", "v2": "blk"})
        self.assertTrue(f.node_up(None, "v0", 0.0))
        self.assertEqual(f.central_commands(None, ["c"]), ["c"])
        self.assertEqual(f.compromised(None), set())
        with self.assertRaises(ValueError):
            faults.build("equivocate")

    def test_reject_reasons_are_unique(self):
        self.assertEqual(len(set(REJECT_REASONS)), len(REJECT_REASONS))


class TestExperimentCommon(unittest.TestCase):
    def test_write_results(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "sub", "exp")
            with contextlib.redirect_stdout(io.StringIO()):
                common.write_results(out, [{"a": 1, "b": 2}, {"a": 3, "c": 4}], {"experiment": "test"})
            with open(out + ".csv", newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            with open(out + ".meta.json", encoding="utf-8") as fh:
                meta = json.load(fh)
        self.assertEqual(list(rows[0]), ["a", "b", "c"])
        self.assertEqual(rows[1]["c"], "4")
        self.assertEqual(meta["experiment"], "test")
        self.assertIn("git_commit", meta)

    def test_args_and_serial_jobs(self):
        a = common.common_args().parse_args(["--quick"])
        self.assertEqual(common.sizes(a), (2, 3))
        self.assertEqual(common.run_jobs(abs, [-1, 2, -3]), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
