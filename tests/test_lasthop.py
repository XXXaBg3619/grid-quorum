"""The last hop (gridq/lasthop.py): rotating dispatch, delivery to the substations,
gap sync, state reports, and replays end to end (experiment 4 in small)."""
import os
import sys
import unittest
from collections import Counter, defaultdict
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.crypto import KeyRing  # noqa: E402
from gridq.events import EventKind  # noqa: E402
from gridq.faults import Honest, Replay, build  # noqa: E402
from gridq.lasthop import RotatingDispatch, default_feeders, report_digest, run_lasthop  # noqa: E402
from gridq.metrics import commands_executed, feeder_timelines, injected_outcomes, state_deviation_s  # noqa: E402

CFG = GridConfig()
FEEDERS = default_feeders()
SUBS = sorted(FEEDERS)
V = [f"v{i}" for i in range(7)]
NO_DEVIATION = {"unsafe_on": 0.0, "wrong_off": 0.0}


def longest_chain(hop):
    return max(len(n.chain) for n in hop.com.nodes.values())


def rejected(hop, sub=None):
    return Counter(e.data["reason"] for e in hop.log.of_kind(EventKind.COMMAND_REJECTED)
                   if sub is None or e.data["substation"] == sub)


class TestRotatingDispatch(unittest.TestCase):
    def setUp(self):
        self.keys = KeyRing(SUBS, CFG.seed)
        self.d = RotatingDispatch(CFG, FEEDERS, self.keys)
        self.start = self.d.start_state()
        self.node = SimpleNamespace(name="stub", chain=[])          # a validator with nothing decided yet

    def report(self, sub, version, states, keys=None):
        r = {"substation": sub, "version": version, "states": states}
        r["sig"] = (keys or self.keys).sign(sub, report_digest(CFG, r))
        return r

    def block(self, reports, slot=0):
        """A block with these reports and the commands every validator computes from them."""
        view = dict(self.start)
        for r in reports:
            view[r["substation"]] = {"version": r["version"], "states": r["states"]}
        return {"slot": slot, "payload": {"reports": reports, "commands": self.d.commands(view, Counter(), slot)}}

    def test_one_group_off_per_slot(self):
        self.assertEqual(self.d.target(0)["S1"], {"S1-F1": "off", "S1-F2": "on", "S1-F3": "on"})
        self.assertEqual(self.d.target(4)["S3"], {"S3-F1": "on", "S3-F2": "off", "S3-F3": "on"})

    def test_commands_are_absolute_numbered_and_versioned(self):
        first = self.d.commands(self.start, Counter(), 0)
        self.assertEqual([(c["substation"], c["feeder"], c["target"], c["seq"], c["state_version"]) for c in first],
                         [(s, f"{s}-F1", "off", 1, 0) for s in SUBS])
        self.assertEqual({c["expires"] for c in first}, {CFG.command_ttl_s})
        view = {s: {"version": 1, "states": self.d.target(0)[s]} for s in SUBS}
        second = [c for c in self.d.commands(view, Counter({s: 1 for s in SUBS}), 1) if c["substation"] == "S1"]
        self.assertEqual([(c["feeder"], c["target"], c["seq"], c["state_version"], c["expires"]) for c in second],
                         [("S1-F1", "on", 2, 1, 1200.0), ("S1-F2", "off", 3, 2, 1200.0)])

    def test_validators_recompute_the_commands(self):
        good = self.block([])
        self.assertTrue(self.d.valid(self.node, good))
        cmds = good["payload"]["commands"]
        flipped = [dict(c, target="on") if c["substation"] == "S2" else c for c in cmds]
        for payload in ({"reports": [], "commands": flipped},
                        {"reports": [], "commands": cmds[1:]},                   # one command left out
                        {"reports": [], "commands": cmds, "extra": []}):
            self.assertFalse(self.d.valid(self.node, {"slot": 0, "payload": payload}))
        self.assertFalse(self.d.valid(self.node, dict(good, slot=1)))            # slot 0's commands in slot 1

    def test_reports_must_be_signed_complete_newer_and_one_per_substation(self):
        s1 = {"S1-F1": "off", "S1-F2": "on", "S1-F3": "on"}
        r1, r2 = self.report("S1", 1, s1), self.report("S2", 1, {"S2-F1": "on", "S2-F2": "on", "S2-F3": "off"})
        ok = self.block([r1, r2])
        self.assertTrue(self.d.valid(self.node, ok))
        self.assertEqual([c["substation"] for c in ok["payload"]["commands"]].count("S1"), 0)  # S1 is already there
        self.assertFalse(self.d.valid(self.node, dict(ok, payload=dict(ok["payload"], reports=[r1]))))
        bad = (self.report("S1", 1, s1, keys=KeyRing(SUBS, seed=99)),          # not S1's key
               self.report("S1", 0, s1),                                       # not newer than the chain's view
               self.report("S1", 1, {"S1-F1": "off"}))                         # not every feeder
        for r in bad:
            self.assertFalse(self.d.valid(self.node, self.block([r])), r)
        self.assertFalse(self.d.valid(self.node, self.block([r1, r1])))
        self.assertFalse(self.d.valid(self.node, self.block([r2, r1])))


class TestHonestIsland(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hop = run_lasthop(CFG, heights=12)

    def test_every_committed_command_runs_once(self):
        hop = self.hop
        self.assertEqual(longest_chain(hop), 12)
        issued, executed = commands_executed(hop.log)
        self.assertGreater(issued, 0)
        self.assertEqual(issued, executed)
        acc = [(e.data["substation"], e.data["seq"]) for e in hop.log.of_kind(EventKind.COMMAND_ACCEPTED)]
        self.assertEqual(len(acc), len(set(acc)))
        self.assertEqual(rejected(hop), Counter())
        for s, sub in hop.subs.items():
            self.assertEqual((sub.v.tip_height, sub.n_sync), (12, 0), s)
            self.assertEqual(sub.v.feeders, hop.dispatch.target(11)[s], s)

    def test_a_command_runs_one_hop_after_its_commit(self):
        issued = {(e.data["substation"], e.data["seq"]): e.t for e in self.hop.log.of_kind(EventKind.COMMAND_ISSUED)}
        for e in self.hop.log.of_kind(EventKind.COMMAND_ACCEPTED):
            lag = e.t - issued[(e.data["substation"], e.data["seq"])]
            self.assertTrue(0.0 < lag <= CFG.max_hop_s() + 1e-9, lag)

    def test_reports_reach_the_chain(self):
        reports = [r for h in self.hop.com.nodes["v0"].chain for r in h["payload"]["reports"]]
        self.assertEqual({r["substation"] for r in reports}, set(SUBS))
        for s, sub in self.hop.subs.items():
            last = [r for r in reports if r["substation"] == s][-1]
            self.assertLessEqual(last["version"], sub.v.version)

    def test_deterministic_and_log_chained(self):
        cfg = CFG.with_(link_loss_p=0.1)
        a, b = run_lasthop(cfg, heights=6), run_lasthop(cfg, heights=6)
        self.assertEqual(a.log.head, b.log.head)
        self.assertNotEqual(a.log.head, run_lasthop(cfg, run=1, heights=6).log.head)
        self.assertTrue(a.log.verify_chain())


class TestLossyIsland(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hop = run_lasthop(CFG.with_(link_loss_p=0.1, seed=3), heights=24)

    def test_only_committed_fresh_commands_run_in_order(self):
        hop = self.hop
        issued = {(e.data["substation"], e.data["seq"]): e.data for e in hop.log.of_kind(EventKind.COMMAND_ISSUED)}
        last = defaultdict(int)
        for e in hop.log.of_kind(EventKind.COMMAND_ACCEPTED):
            s, seq = e.data["substation"], e.data["seq"]
            c = issued[(s, seq)]
            self.assertEqual((e.data["feeder"], e.data["target"]), (c["feeder"], c["target"]))
            self.assertLessEqual(e.t, c["expires"])
            self.assertGreater(seq, last[s])
            last[s] = seq

    def test_loss_costs_only_refusals_and_the_tips_catch_up(self):
        hop = self.hop
        self.assertLessEqual(set(rejected(hop)), {"state_mismatch", "expired", "stale_seq"})
        for sub in hop.subs.values():
            self.assertEqual(sub.v.tip_height, longest_chain(hop))
        issued, executed = commands_executed(hop.log)
        self.assertGreaterEqual(executed / issued, 0.9)


class TestCutOffSubstation(unittest.TestCase):
    """S2 hears nothing from 600 s to 2100 s: it holds its state, then catches up by sync and reports."""

    @classmethod
    def setUpClass(cls):
        cls.hop = run_lasthop(CFG, heights=14, partitions=[(600.0, 2100.0, [V + ["S1", "S3", "S4"], ["S2"]])])

    def test_holds_its_state_while_cut_off(self):
        switched = [e for e in self.hop.log.of_kind(EventKind.FEEDER_SWITCHED)
                    if e.data["substation"] == "S2" and 600.0 <= e.t < 2100.0]
        self.assertEqual(switched, [])

    def test_catches_up_and_ends_on_the_schedule(self):
        hop = self.hop
        s2 = hop.subs["S2"]
        self.assertGreater(s2.n_sync, 0)
        self.assertEqual(s2.v.tip_height, 14)
        for s, sub in hop.subs.items():
            self.assertEqual(sub.v.feeders, hop.dispatch.target(13)[s], s)
        self.assertTrue(rejected(hop, "S2"))
        self.assertLessEqual(set(rejected(hop, "S2")), {"expired", "state_mismatch"})   # nothing from overlapping syncs
        self.assertEqual(rejected(hop) - rejected(hop, "S2"), Counter())


class TestReplay(unittest.TestCase):
    """Experiment 4 in small: deliveries (block and valid commit proof) recorded and sent again."""
    HEIGHTS = 15            # 75 minutes, so "late" finds deliveries 30 minutes old

    @classmethod
    def setUpClass(cls):
        cls.ref = run_lasthop(CFG, heights=cls.HEIGHTS)
        cls.ref_tl = feeder_timelines(cls.ref.log, cls.ref.initial_states())

    def attack(self, mode, **kw):
        hop = run_lasthop(CFG, heights=self.HEIGHTS, fault=Replay(mode), **kw)
        dev = state_deviation_s(feeder_timelines(hop.log, hop.initial_states()), self.ref_tl, hop.cfg.horizon_s)
        return injected_outcomes(hop.log), dev, hop

    def test_full_verifier_executes_no_replay(self):
        for mode in Replay.MODES:
            inj, dev, hop = self.attack(mode)
            self.assertGreater(inj["commands"], 0, mode)
            self.assertEqual((inj["accepted"], dev), (0, NO_DEVIATION), mode)
            issued, executed = commands_executed(hop.log)
            self.assertEqual(issued, executed, mode)

    def test_a_copy_of_the_newest_block_is_dropped_quietly(self):
        inj, _, _ = self.attack("duplicate")
        self.assertEqual(inj["ignored"], inj["commands"])

    def test_order_or_version_alone_stops_every_replay(self):
        for checks in ({"order"}, {"version"}):
            for mode in Replay.MODES:
                inj, _, _ = self.attack(mode, checks=checks)
                self.assertEqual(inj["accepted"], 0, (checks, mode))

    def test_signature_only_gateway_executes_replays(self):
        inj, dev, _ = self.attack("reverse", checks=())
        self.assertGreater(inj["switched"], 0)
        self.assertGreater(dev["unsafe_on"], 0.0)
        self.assertGreater(dev["wrong_off"], 0.0)

    def test_ttl_alone_needs_old_replays_and_a_right_clock(self):
        inj, _, _ = self.attack("reverse", checks={"ttl"})
        self.assertGreater(inj["accepted"], 0)                               # 10 s old: inside the TTL
        inj, _, _ = self.attack("late", checks={"ttl"})
        self.assertEqual((inj["accepted"], inj.get("rejected:expired")), (0, inj["commands"]))
        inj, _, _ = self.attack("late", checks={"ttl"}, clock_offset_s=-3600.0)
        self.assertGreater(inj["accepted"], 0)

    def test_fault_construction(self):
        self.assertEqual(build("replay", mode="late").describe()["mode"], "late")
        self.assertIsInstance(build("none"), Honest)
        with self.assertRaises(ValueError):
            Replay("rewind")


if __name__ == "__main__":
    unittest.main()
