"""Fault injection (gridq/faults.py) beyond the consensus tests: the equivocation split, the
island's report-inclusion rule against a proposer that leaves state reports out, experiment
5's attackers (a hacked dispatch centre, compromised validators' keys) and experiment 6's
observer and frozen centre."""
import math
import os
import random
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.consensus import block_id  # noqa: E402
from gridq.events import EventKind  # noqa: E402
from gridq.faults import CompromisedValidators, Equivocate, Fault, HackedCentre, Observe, attack_targets  # noqa: E402
from gridq.lasthop import run_lasthop  # noqa: E402
from gridq.loads import Loads, default_island  # noqa: E402
from gridq.metrics import data_exposure  # noqa: E402
from gridq.policy import decide, start_view  # noqa: E402
from gridq.sim import simulate  # noqa: E402

V = [f"v{i}" for i in range(7)]
SPECS = default_island()
CRITICAL = {s.name for s in SPECS if s.critical}
# One hour at a flat 45 % of the peak: the policy sheds feeders from the first slot on.
SHORT = GridConfig(horizon_h=1.0, supply_jitter_h=0.0, supply_profile=((0.0, 0.45), (24.0, 0.45)))
START = 900.0                       # the attack takes over from dispatch slot 3


def events(sim, kind, after=0.0):
    return [e for e in sim.log.of_kind(kind) if e.t >= after]


def issued(sim):
    return [(e.data["expires"], e.data["substation"], e.data["feeder"], e.data["target"])
            for e in sim.log.of_kind(EventKind.COMMAND_ISSUED)]


class OmitReports(Fault):
    """Compromised proposers send everyone the block without the fresh state reports (no split)."""
    label = "omit-test"

    def __init__(self, bad):
        super().__init__(1.0)
        self.bad, self.sent = frozenset(bad), set()

    def compromised(self, sim):
        return set(self.bad)

    def proposal(self, sim, node, height, rnd, block, recipients):
        if node in self.bad:
            stale = Equivocate(self.bad).second_block(sim, node, block)
            if stale is not None:
                block = stale
                self.sent.add(block_id(block))
        return {r: block for r in recipients}


class TestEquivocateSplit(unittest.TestCase):
    def test_compromised_in_the_first_group_honest_halved_per_round(self):
        f = Equivocate({"v5", "v6"})
        f.begin_run(None, random.Random(1))
        g1, g2 = f.split(3, 0, V)
        self.assertLessEqual({"v5", "v6"}, g1)
        self.assertEqual((len(g1 - {"v5", "v6"}), len(g2)), (2, 3))
        self.assertEqual(g1 | g2, set(V))
        self.assertEqual(f.split(3, 0, V), (g1, g2))                    # the same split for every sender
        self.assertTrue(any(f.split(3, r, V) != (g1, g2) for r in range(1, 8)))
        fixed = Equivocate({"v6"}, groups=({"v0", "v6"}, {"v1"}))
        self.assertEqual(fixed.split(1, 0, V), (frozenset({"v0", "v6"}), frozenset({"v1"})))

    def test_inactive_or_honest_nodes_behave(self):
        f = Equivocate({"v6"}, p_run=0.0)
        f.begin_run(None, random.Random(0))
        self.assertEqual(f.proposal(None, "v6", 1, 0, {"x": 1}, ["v0", "v1"]), {"v0": {"x": 1}, "v1": {"x": 1}})
        self.assertEqual(f.compromised(None), set())


class TestReportInclusion(unittest.TestCase):
    """A compromised proposer within f may leave fresh state reports out: the block is valid for the
    older state. Honest validators prevote nil on a new proposal that leaves out a report they have
    held for cfg.report_inclusion_s; the next round's proposer then gets a current block committed."""

    def run_island(self, rule_s):
        cfg = GridConfig(report_inclusion_s=rule_s)
        fault = OmitReports({"v5", "v6"})
        hop = run_lasthop(cfg, fault=fault, heights=16)
        honest = hop.com.honest()
        committed = {c["block"] for d in hop.com.decisions.values() for n, c in d.items() if n in honest}
        return hop, fault, committed, honest

    def test_without_the_rule_stale_blocks_get_committed(self):
        hop, fault, committed, honest = self.run_island(math.inf)
        self.assertTrue(fault.sent & committed)
        self.assertEqual(hop.com.undecided_heights(honest), [])

    def test_with_the_rule_no_stale_block_is_committed_and_every_height_commits(self):
        hop, fault, committed, honest = self.run_island(60.0)
        self.assertTrue(fault.sent)                                    # the attack was tried
        self.assertFalse(fault.sent & committed)
        self.assertEqual(hop.com.undecided_heights(honest), [])
        self.assertEqual(hop.com.conflicting_heights(), [])
        rounds = {h: c["round"] for h, c in hop.com.first_commits().items()}
        self.assertTrue(any(r > 0 for r in rounds.values()))           # a refused stale block costs a round

    def test_a_block_with_every_report_passes_the_rule(self):
        hop = run_lasthop(GridConfig(), heights=8)
        self.assertEqual({c["round"] for c in hop.com.first_commits().values()}, {0})


class TestAttackTargets(unittest.TestCase):
    """What the experiment 5 attacker sends instead of the policy's targets."""

    def setUp(self):
        cfg, loads = GridConfig(), Loads(GridConfig(), SPECS)
        self.cfg, self.view = cfg, start_view(SPECS)
        self.meas = {"supply_kw": 0.45 * loads.peak_kw(),
                     "demand_kw": {s.name: loads.slot_forecast(s.name, 0.0) for s in SPECS}}
        self.policy = decide(cfg, SPECS, self.view, self.meas, 0)

    def targets(self, mode):
        return attack_targets(mode, self.cfg, SPECS, self.view, self.meas, 0)

    def test_restore_all_turns_on_what_the_policy_sheds(self):
        self.assertIn("off", self.policy.values())
        self.assertEqual(set(self.targets("restore_all").values()), {"on"})

    def test_cut_critical_and_starve_change_only_their_feeders(self):
        hits = {"cut_critical": lambda s: s.critical,
                "starve": lambda s: s.substation == "S4" and not s.critical}
        for mode, hit in hits.items():
            t = self.targets(mode)
            for s in SPECS:
                self.assertEqual(t[s.name], "off" if hit(s) else self.policy[s.name], (mode, s.name))

    def test_freeze_keeps_every_feeder_as_it_is(self):
        view = start_view(SPECS)
        view["S2"]["states"]["S2-F3"] = "off"
        t = attack_targets("freeze", self.cfg, SPECS, view, self.meas, 0)
        self.assertEqual(t, {s.name: view[s.substation]["states"][s.name] for s in SPECS})

    def test_unknown_modes_are_refused(self):
        for make in (lambda: self.targets("blackout"), lambda: HackedCentre("blackout"),
                     lambda: CompromisedValidators(["v6"], "blackout")):
            with self.assertRaises(ValueError):
                make()


class TestHackedCentre(unittest.TestCase):
    """A and B: the attack is signed with the centre's own key, so only B's local rule can refuse it."""

    def test_cut_critical_a_executes_b_refuses(self):
        a = simulate(SHORT.with_(arch="A"), HackedCentre("cut_critical", start=START))
        cut = {e.data["feeder"] for e in events(a, EventKind.FEEDER_SWITCHED, START)
               if e.data["feeder"] in CRITICAL and e.data["state"] == "off"}
        self.assertEqual(cut, CRITICAL)
        self.assertEqual(a.fault.sent, len(events(a, EventKind.COMMAND_ACCEPTED, START)))
        b = simulate(SHORT.with_(arch="B"), HackedCentre("cut_critical", start=START))
        self.assertFalse([e for e in events(b, EventKind.FEEDER_SWITCHED) if e.data["feeder"] in CRITICAL])
        reasons = Counter(e.data["reason"] for e in events(b, EventKind.COMMAND_REJECTED, START))
        self.assertGreater(reasons["policy_violation"], 0)
        self.assertGreater(b.log.run_meta["arch"]["enforcer_refused"]["critical_feeder"], 0)

    def test_restore_all_passes_b_local_rule_and_trips_ufls(self):
        for arch in "AB":
            sim = simulate(SHORT.with_(arch=arch), HackedCentre("restore_all", start=START))
            self.assertGreater(sim.fault.sent, 0, arch)
            self.assertEqual(events(sim, EventKind.COMMAND_REJECTED, START), [], arch)
            self.assertTrue(events(sim, EventKind.UFLS_TRIP, START), arch)

    def test_before_the_window_the_centre_is_honest(self):
        honest = simulate(SHORT.with_(arch="A"))
        late = simulate(SHORT.with_(arch="A"), HackedCentre("restore_all", start=SHORT.horizon_s))
        self.assertEqual(issued(late), issued(honest))
        self.assertEqual(late.fault.sent, 0)


class TestCompromisedValidators(unittest.TestCase):
    """C: b validators' keys propose attack blocks and forge commit proofs at every slot."""

    @classmethod
    def setUpClass(cls):
        cfg = SHORT.with_(arch="C")
        cls.honest = simulate(cfg)
        cls.sims = {b: simulate(cfg, CompromisedValidators(V[7 - b:], "restore_all", start=START)) for b in (4, 5)}

    def test_fewer_than_q_keys_change_nothing(self):
        sim = self.sims[4]
        fault, com = sim.fault, sim.link.com
        self.assertTrue(fault.proposed and fault.forged)               # the attack was tried both ways
        committed = {c["block"] for d in com.decisions.values() for c in d.values()}
        self.assertFalse(fault.proposed & committed)
        forged = [e for e in events(sim, EventKind.COMMAND_REJECTED)
                  if str(e.data.get("delivery", "")).startswith("forge:")]
        self.assertEqual({e.data["reason"] for e in forged}, {"insufficient_quorum"})
        self.assertEqual(len(forged), sum(n for _, _, n in fault.forged))
        self.assertEqual(issued(sim), issued(self.honest))
        self.assertEqual(com.conflicting_heights(), [])

    def test_q_keys_capture_the_substations(self):
        sim = self.sims[5]
        run = [e for e in events(sim, EventKind.COMMAND_ACCEPTED)
               if str(e.data.get("delivery", "")).startswith("forge:")]
        self.assertTrue(run)
        self.assertGreaterEqual(min(e.t for e in run), START)
        later = Counter(e.data["reason"] for e in events(sim, EventKind.COMMAND_REJECTED, START)
                        if not str(e.data.get("delivery", "")).startswith("forge:"))
        self.assertTrue(set(later) <= {"stale_seq", "header_mismatch"} and later, later)
        self.assertTrue(events(sim, EventKind.UFLS_TRIP, START))


def switching(sim):
    return [(e.t, e.data["feeder"], e.data["state"], e.data["cause"]) for e in sim.log.of_kind(EventKind.FEEDER_SWITCHED)]


class TestObserveAndFreeze(unittest.TestCase):
    """Experiment 6: Observe records what each node receives and changes nothing; a hacked centre
    that freezes stalls the rotation alone, with no failover and no fallback table."""

    def test_observe_records_the_read_surface_and_changes_nothing(self):
        subs = ["S1", "S2", "S3", "S4"]
        for arch in "AC":
            plain, seen = simulate(SHORT.with_(arch=arch)), simulate(SHORT.with_(arch=arch), Observe())
            self.assertEqual((issued(seen), switching(seen)), (issued(plain), switching(plain)), arch)
            ex = data_exposure(seen.log)
            if arch == "A":
                self.assertEqual(ex["S1"], {"states": ["S1"], "commands": ["S1"], "measurements": False})
                self.assertEqual(ex["centre"], {"states": subs, "commands": subs, "measurements": True})
                self.assertEqual(ex["backup"], {"states": subs, "commands": [], "measurements": False})
            else:
                self.assertEqual(sorted(ex), subs + V)
                for node, r in ex.items():
                    self.assertEqual(r, {"states": subs, "commands": subs, "measurements": True}, node)

    def test_a_frozen_centre_stalls_the_rotation_alone(self):
        honest = simulate(SHORT.with_(arch="A"))
        self.assertTrue(events(honest, EventKind.COMMAND_ACCEPTED, START + 60.0))
        for arch in "AB":
            sim = simulate(SHORT.with_(arch=arch), HackedCentre("freeze", start=START))
            self.assertEqual(sim.fault.sent, 0, arch)
            self.assertEqual(events(sim, EventKind.COMMAND_ACCEPTED, START + 60.0), [], arch)
            self.assertTrue(events(sim, EventKind.DECISION_APPLIED, START + 60.0), arch)    # alive tokens verify
            self.assertFalse([e for e in sim.log.of_kind(EventKind.FAILOVER) if e.data.get("action") == "take_over"])
            self.assertFalse(list(sim.log.of_kind(EventKind.FALLBACK_SCHEDULE)), arch)


if __name__ == "__main__":
    unittest.main()
