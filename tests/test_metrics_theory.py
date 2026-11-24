"""Ported statistics helpers and the lossy-round reference value."""
import math
import os
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.metrics import jain, mean_ci95, paired_diff  # noqa: E402
from gridq.theory import commit_latency_bound, round_outcome, round_success_prob  # noqa: E402


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

    def test_only_validators_with_the_proposal_precommit(self):
        # n = 4, q = 3, proposer 0. The proposal misses validator 3, and 2's prevote to 1 is
        # lost: 0 and 2 count three prevotes, 1 counts two. Validator 3 counts three prevotes
        # but has no proposal, so it may not precommit: two precommits < q, no commit
        # (evsim's model let 3 precommit and committed).
        got = lambda kind, i, j: not ((kind, j) == ("proposal", 3) or (kind, i, j) == ("prevote", 2, 1))
        self.assertFalse(round_outcome(4, 3, 0, got))
        self.assertTrue(round_outcome(4, 3, 0, lambda kind, i, j: (kind, j) != ("proposal", 3)))


class TestLatencyBound(unittest.TestCase):
    def test_three_hops_plus_each_silent_round(self):
        cfg = GridConfig()                       # timeouts 3 s / 1 s, +0.5 s per round; hop <= 0.1 s
        self.assertAlmostEqual(commit_latency_bound(cfg), 0.3)
        self.assertAlmostEqual(commit_latency_bound(cfg, 1), 3.0 + 2 * 1.0 + 0.3)
        self.assertAlmostEqual(commit_latency_bound(cfg, 2), (3.0 + 2.0) + (3.5 + 3.0) + 0.3)


if __name__ == "__main__":
    unittest.main()
