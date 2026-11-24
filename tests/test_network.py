"""Network model: outage model and keyed draws (ported from evsim tests/test_e3_v2.py and
tests/test_m1_network.py), plus what is new here: links between any two nodes, partitions."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.network import Network, all_down_prob, either_down_prob  # noqa: E402


def outage_cfg(**kw):
    base = dict(link_loss_p=0.1, link_loss_model="outage", link_outage_mean_s=600.0)
    base.update(kw)
    return GridConfig(**base)


class TestOutageModel(unittest.TestCase):
    def test_share_of_time_down_matches_p(self):
        cfg, down, n = outage_cfg(), 0, 0
        rng = random.Random(0)
        for run in range(400):
            net = Network(cfg.with_(seed=run), run)
            for _ in range(20):
                down += net.is_down("v1", rng.uniform(0, cfg.horizon_s))
                n += 1
        self.assertAlmostEqual(down / n, 0.1, delta=0.02)

    def test_retries_barely_help_during_outages(self):
        iid = GridConfig(link_loss_p=0.1, link_retries=2)
        self.assertAlmostEqual(Network(iid).effective_loss(), 1e-3)
        self.assertAlmostEqual(all_down_prob(iid, [0, 1, 2]), 1e-3)
        one_end = Network(outage_cfg(link_retries=2)).effective_loss(both_ends=False)
        self.assertGreater(one_end, 0.09)     # a 2 s retry window rarely outlasts a 10-minute outage

    def test_both_ends_formula_matches_simulation(self):
        cfg = outage_cfg(link_retries=2)
        expect = Network(cfg).effective_loss()            # either end down at all three attempts
        self.assertAlmostEqual(either_down_prob(cfg, [0.0]), 1 - 0.9 ** 2)
        self.assertGreater(expect, Network(cfg).effective_loss(both_ends=False))
        lost = n = 0
        rng = random.Random(1)
        for run in range(300):
            net = Network(cfg.with_(seed=run), run)
            for i in range(30):
                lost += net.deliver(f"m{i}", "v0", "v1", rng.uniform(0, cfg.horizon_s - 10)) is None
                n += 1
        self.assertAlmostEqual(lost / n, expect, delta=0.02)

    def test_paired_runs_share_outages(self):
        cfg = outage_cfg(seed=3)
        a, b = Network(cfg, 5), Network(cfg.with_(arch="C"), 5)   # another architecture, same seed and run
        self.assertEqual(a.outages("v2"), b.outages("v2"))
        b.deliver("extra", "v0", "v4", 100.0)                      # an extra message in one run ...
        ts = [i * 997.0 for i in range(80)]
        self.assertEqual([a.deliver(f"m{i}", "v2", "v3", t) for i, t in enumerate(ts)],
                         [b.deliver(f"m{i}", "v2", "v3", t) for i, t in enumerate(ts)])   # ... shifts nothing

    def test_outage_nodes_limit_who_goes_down(self):
        net = Network(outage_cfg(link_loss_p=1.0), outage_nodes=["v0"])
        self.assertTrue(net.is_down("v0", 10.0))
        self.assertFalse(net.is_down("v1", 10.0))
        self.assertIsNone(net.deliver("m", "v0", "v1", 10.0))
        self.assertIsNotNone(net.deliver("m", "v1", "v2", 10.0))


class TestIidAndDelay(unittest.TestCase):
    def test_iid_loss_rate_and_keyed_draws(self):
        cfg = GridConfig(link_loss_p=0.2, seed=4)
        lost = lambda net: [net.deliver(f"m{i}", "v0", "v1", 0.0) is None for i in range(5000)]
        first = lost(Network(cfg, 0))
        self.assertAlmostEqual(sum(first) / len(first), 0.2, delta=0.02)
        self.assertEqual(first, lost(Network(cfg, 0)))        # same seed and run: same losses
        self.assertNotEqual(first, lost(Network(cfg, 1)))     # another run: other losses

    def test_retries_recover_iid_loss(self):
        net = Network(GridConfig(link_loss_p=0.3, link_retries=3, link_retry_gap_s=1.0))
        res = [net.deliver(f"m{i}", "v0", "v1", 0.0) for i in range(4000)]
        self.assertAlmostEqual(sum(r is None for r in res) / len(res), 0.3 ** 4, delta=0.01)
        self.assertTrue(any(r is not None and r >= 1.0 for r in res))     # some got through on a resend

    def test_delay_within_bounds(self):
        net = Network(GridConfig(net_delay_s=0.05, net_jitter_s=0.05))
        ds = [net.deliver(f"m{i}", "v0", "v1", 10.0) - 10.0 for i in range(500)]
        self.assertTrue(all(0.05 <= d <= 0.1 + 1e-12 for d in ds))
        self.assertGreater(max(ds) - min(ds), 0.03)

    def test_message_to_self_is_local(self):
        self.assertEqual(Network(GridConfig(link_loss_p=1.0)).deliver("m", "v0", "v0", 3.0), 3.0)


class TestPartition(unittest.TestCase):
    def test_blocks_only_across_groups_and_only_while_active(self):
        net = Network(GridConfig())
        net.add_partition(100.0, 200.0, [["v0", "v1", "v2", "v3"], ["v4", "v5"]])
        self.assertIsNotNone(net.deliver("a", "v0", "v3", 150.0))   # same group
        self.assertIsNone(net.deliver("b", "v0", "v4", 150.0))      # across groups
        self.assertIsNone(net.deliver("c", "v6", "v0", 150.0))      # v6 is in no group: cut off
        self.assertIsNotNone(net.deliver("d", "v0", "v4", 99.0))    # before
        self.assertIsNotNone(net.deliver("e", "v0", "v4", 200.0))   # healed (the end is exclusive)

    def test_resend_after_heal_gets_through(self):
        net = Network(GridConfig(link_retries=2, link_retry_gap_s=5.0))
        net.add_partition(0.0, 8.0, [["v0"], ["v1"]])
        at = net.deliver("m", "v0", "v1", 0.0)        # attempts at 0 and 5 s are cut, 10 s gets through
        self.assertIsNotNone(at)
        self.assertGreaterEqual(at, 10.0)


if __name__ == "__main__":
    unittest.main()
