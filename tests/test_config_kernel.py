"""GridConfig (quorum rule, validation) and the discrete-event kernel."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.kernel import PRIO_DISPATCH, PRIO_MEASURE, PRIO_MESSAGE, PRIO_PHYSICS, Kernel  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_quorum_follows_n(self):
        for n, f, q in [(4, 1, 3), (7, 2, 5), (10, 3, 7), (6, 1, 5)]:
            c = GridConfig(n_validators=n)
            self.assertEqual((c.f, c.quorum), (f, q), n)

    def test_quorum_safety_and_liveness(self):
        for n in range(4, 40):
            c = GridConfig(n_validators=n)
            # any two quorums share at least f + 1 validators, so at least one honest one
            self.assertGreaterEqual(2 * c.quorum - n, c.f + 1, n)
            # f silent or unreachable validators cannot block a commit
            self.assertLessEqual(c.quorum, n - c.f, n)

    def test_validation(self):
        with self.assertRaises(ValueError):
            GridConfig(arch="E")
        with self.assertRaises(ValueError):
            GridConfig(arch="D", n_validators=3)
        GridConfig(arch="A", n_validators=1)          # A has no committee
        with self.assertRaises(ValueError):
            GridConfig(link_loss_model="burst")
        with self.assertRaises(ValueError):
            GridConfig(link_loss_p=1.5)
        with self.assertRaises(ValueError):
            GridConfig().with_(arch="X")              # with_() validates too

    def test_derived_values_and_dict(self):
        c = GridConfig()
        self.assertEqual(c.n_dispatches, 288)
        self.assertAlmostEqual(c.round_timeout(2), c.round_timeout_s + 2 * c.timeout_delta_s)
        d = c.to_dict()
        self.assertEqual((d["f"], d["quorum"], d["arch"]), (2, 5, "D"))
        self.assertEqual(c.with_(n_validators=4).quorum, 3)
        self.assertEqual(c.n_validators, 7)           # frozen: with_() made a copy


class TestKernel(unittest.TestCase):
    def test_same_time_priority_then_fifo(self):
        k, out = Kernel(100.0), []
        k.schedule(5.0, PRIO_MEASURE, out.append, "measure")
        k.schedule(5.0, PRIO_MESSAGE, out.append, "msg1")
        k.schedule(5.0, PRIO_PHYSICS, out.append, "physics")
        k.schedule(5.0, PRIO_MESSAGE, out.append, "msg2")
        k.schedule(1.0, PRIO_DISPATCH, out.append, "early")
        k.run()
        self.assertEqual(out, ["early", "physics", "msg1", "msg2", "measure"])
        self.assertEqual(k.now, 100.0)

    def test_horizon_and_incremental_runs(self):
        k, out = Kernel(10.0), []
        for t in (2.0, 6.0, 12.0):
            k.schedule(t, PRIO_MESSAGE, out.append, t)
        k.run(until=4.0)
        self.assertEqual((out, k.now), ([2.0], 4.0))
        k.run()
        self.assertEqual(out, [2.0, 6.0])             # 12 s is past the horizon
        self.assertEqual(k.pending(), 1)

    def test_callbacks_schedule_more_and_past_is_refused(self):
        k, out = Kernel(10.0), []

        def tick(i):
            out.append((k.now, i))
            if i < 3:
                k.after(1.0, PRIO_DISPATCH, tick, i + 1)

        k.schedule(0.0, PRIO_DISPATCH, tick, 0)
        k.run()
        self.assertEqual(out, [(0.0, 0), (1.0, 1), (2.0, 2), (3.0, 3)])
        with self.assertRaises(ValueError):
            k.schedule(5.0, PRIO_MESSAGE, out.append, "late")


if __name__ == "__main__":
    unittest.main()
