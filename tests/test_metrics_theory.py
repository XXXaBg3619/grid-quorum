"""Ported statistics helpers and the lossy-round reference value."""
import math
import os
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.metrics import jain, mean_ci95, paired_diff  # noqa: E402
from gridq.theory import round_success_prob  # noqa: E402


class TestMetrics(unittest.TestCase):
    def test_mean_ci95(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        m, h = mean_ci95(xs)
        self.assertAlmostEqual(m, 3.0)
        self.assertAlmostEqual(h, 2.776 * statistics.stdev(xs) / math.sqrt(5))
        self.assertEqual(mean_ci95([7.0]), (7.0, 0.0))

    def test_jain(self):
        self.assertAlmostEqual(jain([1, 1, 1, 1]), 1.0)
        self.assertAlmostEqual(jain([1, 0, 0, 0]), 0.25)
        self.assertEqual(jain([]), 1.0)

    def test_paired_diff_positive_means_damage(self):
        honest = {"bad_accept_rate": 0.0, "availability": 0.99}
        faulty = {"bad_accept_rate": 0.1, "availability": 0.90}
        d = paired_diff(faulty, honest, ["bad_accept_rate", "availability"], higher_is_better=["availability"])
        self.assertAlmostEqual(d["bad_accept_rate"], 0.1)
        self.assertAlmostEqual(d["availability"], 0.09)


class TestRoundSuccess(unittest.TestCase):
    def test_no_loss_always_commits(self):
        self.assertEqual(round_success_prob(7, 5, 0.0), 1.0)

    def test_falls_with_loss_and_is_deterministic(self):
        ps = [round_success_prob(7, 5, p, 4000) for p in (0.01, 0.05, 0.1, 0.3)]
        self.assertTrue(all(a >= b for a, b in zip(ps, ps[1:])), ps)
        self.assertEqual(ps[1], round_success_prob(7, 5, 0.05, 4000))
        self.assertLess(ps[-1], 0.9)

    def test_larger_quorum_is_more_fragile(self):
        self.assertGreater(round_success_prob(7, 4, 0.2, 4000), round_success_prob(7, 5, 0.2, 4000))


if __name__ == "__main__":
    unittest.main()
