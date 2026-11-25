"""Ported statistics helpers, the lossy-round reference value, the counting and common-cause
model of experiments 3 and 6, and the metrics for a fault window and for experiment 6."""
import math
import os
import random
import statistics
import sys
import unittest
from itertools import product

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.events import EventKind, EventLog  # noqa: E402
from gridq.metrics import (break_difficulty, data_exposure, jain, mean_ci95, paired_diff, risk_model,  # noqa: E402
                           state_deviation_s, window_metrics)
from gridq.theory import (binom_tail, break_even_weak_risk, commit_latency_bound, compromise_prob,  # noqa: E402
                          compromise_prob_events, poisson_binom_tail, quorum_up_prob, round_outcome,
                          round_success_prob, sample_compromised)

D_RISKS = [0.02, 0.1, 0.1, 0.1, 0.02, 0.1, 0.02]       # D's 7 organisations: 3 strong, 4 weak


def enumerate_prob(k, events, p_nodes):
    """P(at least k nodes fall), summing every outcome of every event and node."""
    n, total = len(p_nodes), 0.0
    for ev in product((0, 1), repeat=len(events)):
        for nd in product((0, 1), repeat=n):
            w = math.prod(p if x else 1.0 - p for (p, _), x in zip(events, ev))
            w *= math.prod(p if x else 1.0 - p for p, x in zip(p_nodes, nd))
            fallen = {j for j in range(n) if nd[j]} | {j for (_, ns), x in zip(events, ev) if x for j in ns}
            total += w * (len(fallen) >= k)
    return total


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

    def test_state_deviation_counts_both_sides(self):
        ref = {("S1", "S1-F1"): [(0.0, "on"), (100.0, "off"), (200.0, "on")]}
        act = {("S1", "S1-F1"): [(0.0, "on"), (150.0, "off"), (260.0, "on")]}
        self.assertEqual(state_deviation_s(act, ref, 300.0), {"unsafe_on": 50.0, "wrong_off": 60.0})
        self.assertEqual(state_deviation_s({}, ref, 300.0), {"unsafe_on": 100.0, "wrong_off": 0.0})
        self.assertEqual(state_deviation_s(ref, ref, 300.0), {"unsafe_on": 0.0, "wrong_off": 0.0})


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


class TestCounting(unittest.TestCase):
    def test_binomial_and_poisson_binomial_tails(self):
        self.assertAlmostEqual(binom_tail(7, 0.1, 3),
                               1.0 - sum(math.comb(7, j) * 0.1 ** j * 0.9 ** (7 - j) for j in range(3)))
        self.assertAlmostEqual(poisson_binom_tail([0.1] * 7, 3), binom_tail(7, 0.1, 3))
        self.assertEqual((poisson_binom_tail([0.3, 0.6], 0), poisson_binom_tail([0.3, 0.6], 3)), (1.0, 0.0))
        self.assertAlmostEqual(poisson_binom_tail([0.3, 0.6], 2), 0.18)
        self.assertAlmostEqual(quorum_up_prob(7, 5, 0.1), binom_tail(7, 0.9, 5))
        self.assertEqual(quorum_up_prob(7, 5, 0.0), 1.0)

    def test_common_cause_events_match_enumeration(self):
        cases = ([], [(0.01, range(7))], [(0.01, range(7)), (0.05, range(7))],
                 [(0.01, range(4)), (0.01, range(4, 7))], [(0.3, [0, 1]), (0.2, [1, 2, 3])])
        for events in cases:
            evs = [(p, list(ns)) for p, ns in events]
            for k in range(9):
                self.assertAlmostEqual(compromise_prob_events(k, evs, D_RISKS), enumerate_prob(k, evs, D_RISKS),
                                       places=12, msg=(events, k))
        for k in (1, 3, 5):
            self.assertAlmostEqual(compromise_prob(k, 0.05, D_RISKS), compromise_prob_events(k, [(0.05, range(7))], D_RISKS))
            # two events over the same nodes act as one with 1 - (1 - a)(1 - b)
            self.assertAlmostEqual(compromise_prob_events(k, [(0.01, range(7)), (0.05, range(7))], D_RISKS),
                                   compromise_prob(k, 1.0 - 0.99 * 0.95, D_RISKS))

    def test_a_draw_follows_the_model(self):
        rng, n = random.Random(3), 20000
        events, ps = [(0.2, [0, 1, 2])], [0.1] * 5
        draws = [len(sample_compromised(events, ps, rng)) for _ in range(n)]
        for k in (1, 3, 4):
            p = compromise_prob_events(k, events, ps)
            self.assertLess(abs(sum(d >= k for d in draws) / n - p), 4.0 * math.sqrt(p * (1.0 - p) / n), k)

    def test_break_even_weak_risk(self):
        w = break_even_weak_risk(3, 0.05, 0.02, 0.01, 7, 4)
        self.assertAlmostEqual(compromise_prob(3, 0.01, [0.02] * 3 + [w] * 4), compromise_prob(3, 0.05, [0.02] * 7),
                               places=7)
        self.assertEqual(break_even_weak_risk(3, 0.01, 0.02, 0.01, 7, 4), 0.02)     # equal common causes
        self.assertEqual(break_even_weak_risk(3, 0.005, 0.02, 0.01, 7, 4), 0.0)     # D's common cause is higher
        self.assertEqual(break_even_weak_risk(5, 0.3, 0.02, 0.01, 7, 4), 1.0)       # D also needs a strong node
        self.assertLess(break_even_weak_risk(1, 0.05, 0.02, 0.01, 7, 4), w)         # reading takes one node
        self.assertGreater(break_even_weak_risk(3, 0.05, 0.02, 0.01, 7, 2), w)      # fewer weak organisations


class TestWindowAndExperiment6Metrics(unittest.TestCase):
    def test_window_metrics(self):
        log = EventLog()
        log.append(10.0, EventKind.DECISION, "centre", {"slot": 0})
        log.append(10.5, EventKind.DECISION_APPLIED, "substation:S1", {"substation": "S1", "slot": 0})
        log.append(305.0, EventKind.DECISION, "centre", {"slot": 1})
        log.append(320.0, EventKind.FEEDER_SWITCHED, "substation:S1", {"cause": "ufls"})
        log.append(330.0, EventKind.FEEDER_SWITCHED, "substation:S1", {"cause": "command"})
        log.append(700.0, EventKind.DECISION_APPLIED, "substation:S1", {"substation": "S1", "slot": 2})
        self.assertEqual(window_metrics(log, GridConfig(), 300.0, 600.0),
                         {"decisions_in": 1, "applied_in": 0, "resume_s": 100.0, "decision_p95_s": 10.0,
                          "switched_in:command": 1, "switched_in:table": 0, "switched_in:ufls": 1})
        self.assertEqual(window_metrics(log, GridConfig(), 300.0, math.inf)["resume_s"], "")

    def test_data_exposure_reads_the_observer_events(self):
        log = EventLog()
        for node, what, sub in (("v0", "states", "S1"), ("v0", "states", "S2"), ("v0", "commands", "S2"),
                                ("v0", "measurements", None), ("S1", "states", "S1")):
            log.append(0.0, EventKind.FAULT_ACTION, "observer",
                       {"action": "observed", "node": node, "what": what, "substation": sub}, "truth")
        log.append(0.0, EventKind.FAULT_ACTION, "attacker", {"action": "inject"}, "truth")
        self.assertEqual(data_exposure(log), {
            "S1": {"states": ["S1"], "commands": [], "measurements": False},
            "v0": {"states": ["S1", "S2"], "commands": ["S2"], "measurements": True}})

    def test_break_difficulty_uses_the_organisations(self):
        th = {"read": 1, "stall": 3, "forge": 5}
        c = {f"v{i}": "taipower" for i in range(7)}
        d = {f"v{i}": f"org{i}" for i in range(7)}
        d_risk = {f"org{i}": p for i, p in enumerate(D_RISKS)}
        got = break_difficulty(c, th, {"taipower": 0.02}, {"taipower": 0.05}, [(0.01, None)])
        for k, n in th.items():
            self.assertAlmostEqual(got[k], compromise_prob(n, 1.0 - 0.99 * 0.95, [0.02] * 7), places=12)
        got = break_difficulty(d, th, d_risk, {"org1": 0.5}, [(0.01, None)])  # one node: no organisation event
        for k, n in th.items():
            self.assertAlmostEqual(got[k], compromise_prob(n, 0.01, D_RISKS), places=12)
        nodes, events, ps = risk_model(d, d_risk, None, [(0.01, ["v0", "v1"]), (0.02, None)])
        self.assertEqual((nodes, events, ps), (sorted(d), [(0.01, [0, 1]), (0.02, list(range(7)))], D_RISKS))


if __name__ == "__main__":
    unittest.main()
