"""The simulator end to end (gridq/sim.py, central.py, arch/, run.py): the four architectures on
one island, centre failover, B's fallback table, committee losses, a hacked centre, and
question 22 in small (a table planned for 40 % in an island left with 25 %)."""
import os
import sys
import unittest
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.events import EventKind  # noqa: E402
from gridq.faults import Fault, Offline  # noqa: E402
from gridq.metrics import island_metrics  # noqa: E402
from gridq.run import run_paired, summarize  # noqa: E402
from gridq.sim import simulate  # noqa: E402
from gridq.verifier import make_command  # noqa: E402

CFG = GridConfig(horizon_h=8.0)
LOSS = 3650.0                       # a site is lost in the middle of dispatch slot 12


def events(sim, kind):
    return list(sim.log.of_kind(kind))


def switches(sim, cause=None, after=0.0):
    return [e for e in events(sim, EventKind.FEEDER_SWITCHED)
            if (cause is None or e.data["cause"] == cause) and e.t >= after]


def sequence(sim):
    """Each feeder's switching sequence (states, not times)."""
    out = defaultdict(list)
    for e in events(sim, EventKind.FEEDER_SWITCHED):
        out[e.data["feeder"]].append(e.data["state"])
    return dict(out)


class TestHonestArchitectures(unittest.TestCase):
    """No fault: the same policy on the same inputs gives the same decisions on all four paths."""

    @classmethod
    def setUpClass(cls):
        cls.sims = {a: simulate(CFG.with_(arch=a)) for a in "ABCD"}
        cls.metrics = {a: summarize(s) for a, s in cls.sims.items()}

    def test_same_commands_and_switching_everywhere(self):
        def cmds(sim):
            return sorted((e.data["substation"], e.data["seq"], e.data["feeder"], e.data["target"])
                          for e in events(sim, EventKind.COMMAND_ISSUED))
        ref = cmds(self.sims["A"])
        self.assertGreater(len(ref), 10)
        for a, sim in self.sims.items():
            self.assertEqual(cmds(sim), ref, a)
            self.assertEqual(sequence(sim), sequence(self.sims["A"]), a)

    def test_every_command_runs_on_time_and_the_island_holds(self):
        for a, m in self.metrics.items():
            self.assertEqual(m["issued"], m["executed"], a)
            self.assertEqual((m["availability"], m["decided_share"], m["critical_served_share"]), (1.0, 1.0, 1.0), a)
            self.assertEqual((m["collapses"], m["failovers"], m["fallback_starts"]), (0, 0, 0), a)
            self.assertFalse([k for k in m if k.startswith("rejected:")], a)
        self.assertEqual(len({round(m["served_share"], 9) for m in self.metrics.values()}), 1)

    def test_commands_follow_the_reports_and_switch_through_the_island(self):
        for a, sim in self.sims.items():
            for f, st in sim.island.state.items():
                sub = sim.link.subs[f.split("-")[0]]
                self.assertEqual(sub.v.feeders[f], st, (a, f))
            self.assertTrue(all(e.data["cause"] == "command" for e in switches(sim)), a)

    def test_log_is_chained_and_deterministic(self):
        for a in "AC":
            again = simulate(CFG.with_(arch=a))
            self.assertEqual(again.log.head, self.sims[a].log.head, a)
            self.assertTrue(again.log.verify_chain())
        self.assertNotEqual(simulate(CFG.with_(arch="A"), run=1).log.head, self.sims["A"].log.head)
        meta = self.sims["D"].log.run_meta
        self.assertEqual(len(meta["island"]), 12)
        self.assertEqual(meta["arch"]["name"], "D")
        self.assertEqual(len(set(meta["arch"]["orgs"].values())), 7)
        self.assertEqual(set(self.sims["C"].log.run_meta["arch"]["orgs"].values()), {"taipower"})


class TestCentres(unittest.TestCase):
    def test_backup_takes_over_without_a_missed_slot(self):
        sim = simulate(CFG.with_(arch="A", horizon_h=3.0), Offline(["centre"], start=LOSS))
        (fo,) = events(sim, EventKind.FAILOVER)
        self.assertEqual((fo.data["centre"], fo.data["action"], fo.data["term"]), ("backup", "take_over", 1))
        self.assertTrue(LOSS + CFG.failover_s - CFG.heartbeat_s <= fo.t <= LOSS + CFG.failover_s + 2 * CFG.heartbeat_s)
        issuers = {e.data["slot"]: e.data["issuer"] for e in events(sim, EventKind.DECISION)}
        self.assertEqual({issuers[k] for k in range(0, 13)}, {"centre"})
        self.assertEqual({issuers[k] for k in range(13, 36)}, {"backup"})
        self.assertEqual(summarize(sim)["availability"], 1.0)

    def test_a_centre_back_from_being_down_restarts_as_standby(self):
        sim = simulate(CFG.with_(arch="A", horizon_h=4.0), Offline(["centre"], start=LOSS, end=7300.0))
        self.assertEqual(sim.link.active_centres(), ["backup"])
        self.assertEqual(sim.link.stats["restarts"], 1)
        late = [e for e in events(sim, EventKind.DECISION) if e.t > 7300.0]
        self.assertTrue(late and all(e.data["issuer"] == "backup" for e in late))

    def test_both_centres_lost_a_holds_b_runs_its_table(self):
        a = simulate(CFG.with_(arch="A"), Offline(["centre", "backup"], start=LOSS))
        self.assertEqual([e for e in events(a, EventKind.DECISION) if e.t > LOSS], [])
        self.assertEqual(switches(a, "command", after=LOSS + 1.0), [])
        self.assertEqual(switches(a, "table"), [])
        b = simulate(CFG.with_(arch="B"), Offline(["centre", "backup"], start=LOSS))
        starts = [e for e in events(b, EventKind.FALLBACK_SCHEDULE) if e.data["action"] == "start"]
        self.assertEqual(sorted(e.data["substation"] for e in starts), ["S1", "S2", "S3", "S4"])
        first = (int(LOSS // CFG.dispatch_period_s) * CFG.dispatch_period_s)   # last word: slot 12's batch
        for e in starts:
            self.assertGreaterEqual(e.t, first + CFG.fallback_after_s)
            self.assertLess(e.t, first + CFG.fallback_after_s + 2 * CFG.dispatch_period_s)
        self.assertTrue(switches(b, "table"))
        self.assertEqual(switches(b, "command", after=LOSS + 1.0), [])

    def test_b_leaves_the_table_when_a_centre_is_back(self):
        back = 3 * 3600.0
        b = simulate(CFG.with_(arch="B", horizon_h=5.0), Offline(["centre", "backup"], start=LOSS, end=back))
        stops = [e for e in events(b, EventKind.FALLBACK_SCHEDULE) if e.data["action"] == "stop"]
        self.assertEqual(len(stops), 4)
        last_stop = max(e.t for e in stops)
        self.assertLess(last_stop, back + CFG.failover_s + CFG.heartbeat_s + CFG.dispatch_period_s + 1.0)
        self.assertEqual(switches(b, "table", after=last_stop), [])
        self.assertEqual(len(b.link.active_centres()), 1)


class TestCommittee(unittest.TestCase):
    def test_f_validators_lost_change_nothing(self):
        sim = simulate(CFG.with_(arch="C"), Offline(["v5", "v6"], start=LOSS))
        m = summarize(sim)
        self.assertEqual((m["availability"], m["decided_share"]), (1.0, 1.0))
        self.assertEqual(sequence(sim), sequence(simulate(CFG.with_(arch="C"))))

    def test_more_than_f_lost_the_substations_hold(self):
        sim = simulate(CFG.with_(arch="D"), Offline(["v4", "v5", "v6"], start=LOSS))
        last = max(e.data["slot"] for e in events(sim, EventKind.DECISION))
        self.assertLessEqual(last, 12)
        self.assertEqual(switches(sim, "command", after=LOSS + CFG.dispatch_period_s), [])
        self.assertLess(summarize(sim)["availability"], 0.5)


class CutHospital(Fault):
    """A hacked centre adds 'S1-F1 off' (the hospital feeder) at slot 2, numbered to pass every check."""
    label = "cut-hospital-test"

    def central_commands(self, sim, centre, slot, commands):
        if slot != 2:
            return commands
        view = sim.view(sim.centres[centre])["S1"]
        mine = [c for c in commands if c["substation"] == "S1"]
        seq = max([sim.centres[centre].issued["S1"], view["last_seq"]] + [c["seq"] for c in mine]) + 1
        return commands + [make_command("S1", "S1-F1", "off", seq, view["version"] + len(mine),
                                        slot * sim.cfg.dispatch_period_s + sim.cfg.command_ttl_s)]


class TestHackedCentre(unittest.TestCase):
    def test_b_refuses_a_cut_hospital_feeder_a_executes_it(self):
        cfg = CFG.with_(horizon_h=1.0)
        a = simulate(cfg.with_(arch="A"), CutHospital())
        self.assertEqual([e.data["state"] for e in switches(a) if e.data["feeder"] == "S1-F1"][:1], ["off"])
        b = simulate(cfg.with_(arch="B"), CutHospital())
        rej = [e for e in events(b, EventKind.COMMAND_REJECTED) if e.data["feeder"] == "S1-F1"]
        self.assertEqual([e.data["reason"] for e in rej], ["policy_violation"])
        self.assertEqual([e for e in switches(b) if e.data["feeder"] == "S1-F1"], [])
        self.assertEqual(b.log.run_meta["arch"]["enforcer_refused"], {"critical_feeder": 1})
        c = simulate(cfg.with_(arch="C"), CutHospital())                        # no centre: the hook is never called
        self.assertEqual([e for e in switches(c) if e.data["feeder"] == "S1-F1"], [])


class TestQuestion22(unittest.TestCase):
    """Two sites lost at a strike that leaves 25 % of the peak: B's table (planned for 40 %) overloads
    the island again and again; C and D recompute and only the strike itself trips UFLS."""

    @classmethod
    def setUpClass(cls):
        cls.strike = 3 * 3600.0 + 120.0                                     # two minutes into slot 36
        h = cls.strike / 3600.0
        cfg = CFG.with_(horizon_h=6.0, supply_jitter_h=0.0,
                        supply_profile=((0.0, 0.70), (h, 0.70), (h, 0.25), (24.0, 0.25)))
        fault = Offline(["centre", "backup", "v5", "v6"], start=cls.strike)
        cls.sims = {a: simulate(cfg.with_(arch=a), fault) for a in "ABCD"}

    def ufls_after(self, a, t):
        return [e for e in events(self.sims[a], EventKind.UFLS_TRIP) if e.t > t]

    def test_everyone_trips_at_the_strike(self):
        for a in "ABCD":
            at = [e for e in events(self.sims[a], EventKind.UFLS_TRIP) if abs(e.t - self.strike - 2.0) < 1e-6]
            self.assertTrue(at, a)

    def test_the_table_overloads_and_the_committee_does_not(self):
        later = self.strike + 60.0
        self.assertGreater(len(self.ufls_after("B", later)), 10)
        self.assertTrue(switches(self.sims["B"], "table"))
        for a in "CD":
            self.assertEqual(self.ufls_after(a, later), [], a)
            self.assertEqual(summarize(self.sims[a])["availability"], 1.0, a)
        for a in "ABCD":
            m = island_metrics(self.sims[a].log)
            self.assertEqual((m["collapses"], m["critical_served_share"]), (0, 1.0), a)


class TestRunPaired(unittest.TestCase):
    def test_pairs_share_loads_and_supply(self):
        cfg = GridConfig(arch="A", horizon_h=6.0)
        res = run_paired(cfg, lambda r: Offline(["centre", "backup"], start=LOSS + r), runs=2)
        self.assertEqual(len(res["damage"]), 2)
        for h, f in zip(res["honest"], res["faulty"]):
            self.assertEqual(h["demand_mwh"], f["demand_mwh"])
        self.assertGreater(res["mean"]["availability"][0], 0.0)              # positive = damage
        self.assertEqual(Counter(type(v) for v in res["mean"].values()), Counter({tuple: len(res["mean"])}))


if __name__ == "__main__":
    unittest.main()
