"""Loads (gridq/loads.py) and the island power model (gridq/power.py): feeders, keyed noise,
cold load, G(t), UFLS stages, collapse and black start, and the verifier's local actions."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.events import EventKind, EventLog  # noqa: E402
from gridq.kernel import PRIO_MEASURE, Kernel  # noqa: E402
from gridq.loads import (KINDS, FeederSpec, Loads, by_substation, default_island, island_meta,  # noqa: E402
                         specs_from_meta)
from gridq.power import Island, Supply, cold_factor, supply_scenario  # noqa: E402
from gridq.verifier import Verifier, make_command  # noqa: E402

CFG = GridConfig()
HOUR = 3600.0


class TestFeeders(unittest.TestCase):
    def test_spec_validation_and_properties(self):
        with self.assertRaises(ValueError):
            FeederSpec("X-F1", "X", (("bakery", 10.0),))
        with self.assertRaises(ValueError):
            FeederSpec("X-F1", "X", (("residential", 0.0),))
        f = FeederSpec("X-F1", "X", (("water", 900.0), ("residential", 1100.0)))
        self.assertTrue(f.critical)
        self.assertEqual(f.weight, KINDS["water"].weight)
        self.assertFalse(FeederSpec("X-F2", "X", (("residential", 10.0), ("commercial", 5.0))).critical)

    def test_default_island(self):
        specs = default_island()
        subs = by_substation(specs)
        self.assertEqual(sorted(subs), ["S1", "S2", "S3", "S4"])
        self.assertTrue(all(len(fs) == 3 for fs in subs.values()))
        for s, fs in subs.items():
            crit = [sp for sp in specs if sp.substation == s and sp.critical]
            self.assertEqual(len(crit), 1, s)                       # one critical feeder per substation
        for sp in specs:
            self.assertEqual(sp.ufls_stage == 0, sp.critical, sp.name)   # critical feeders are UFLS-exempt
        self.assertEqual(sorted({sp.ufls_stage for sp in specs} - {0}), [1, 2, 3, 4])
        self.assertEqual(specs_from_meta(island_meta(specs)), specs)


class TestLoads(unittest.TestCase):
    def setUp(self):
        self.specs = default_island()
        self.loads = Loads(CFG, self.specs, run=0)

    def test_keyed_noise(self):
        again, other = Loads(CFG, self.specs, run=0), Loads(CFG, self.specs, run=1)
        ts = [k * 977.0 for k in range(80)]
        self.assertEqual([self.loads.demand("S1-F2", t) for t in ts], [again.demand("S1-F2", t) for t in ts])
        self.assertNotEqual([self.loads.demand("S1-F2", t) for t in ts], [other.demand("S1-F2", t) for t in ts])
        quiet = Loads(CFG.with_(load_noise=0.0), self.specs)
        self.assertEqual(quiet.demand("S2-F3", 5000.0), quiet.forecast("S2-F3", 5000.0))

    def test_noise_is_small_and_centred(self):
        ratios = [self.loads.demand(sp.name, t) / self.loads.forecast(sp.name, t)
                  for sp in self.specs for t in range(0, 86400, 600)]
        mean = sum(ratios) / len(ratios)
        self.assertAlmostEqual(mean, 1.0, delta=0.02)
        self.assertLess(max(abs(r - 1.0) for r in ratios), 0.2)

    def test_forecast_and_planning_demand(self):
        f = self.loads.forecast
        self.assertAlmostEqual(f("S1-F2", 19 * HOUR), 4300.0 * KINDS["residential"].shape[19])
        self.assertAlmostEqual(f("S1-F2", 19.5 * HOUR),
                               4300.0 * (KINDS["residential"].shape[19] + KINDS["residential"].shape[20]) / 2)
        t0 = 6 * HOUR
        self.assertEqual(self.loads.slot_forecast("S1-F2", t0), max(f("S1-F2", t0), f("S1-F2", t0 + 300.0)))
        peak = self.loads.peak_kw()
        self.assertGreater(peak, 0.0)
        self.assertLess(max(self.loads.critical_kw(t) for t in range(0, 86400, 300)), 0.25 * peak)


class TestColdLoadAndSupply(unittest.TestCase):
    def test_cold_factor(self):
        m, d = CFG.cold_load_multiplier, CFG.cold_load_decay_s
        self.assertEqual(cold_factor(CFG, 0.0, 0.0), 1.0)
        self.assertEqual(cold_factor(CFG, 2 * d, 0.0), m)
        self.assertAlmostEqual(cold_factor(CFG, 2 * d, d / 2), 1.0 + (m - 1.0) / 2)
        self.assertEqual(cold_factor(CFG, 2 * d, d), 1.0)
        self.assertAlmostEqual(cold_factor(CFG, d / 4, 0.0), 1.0 + (m - 1.0) / 4)   # a short outage: less pickup

    def test_supply_steps_and_ramps(self):
        s = Supply(((0.0, 100.0), (10.0, 100.0), (10.0, 40.0), (20.0, 40.0), (30.0, 60.0)))
        self.assertEqual((s.value(9.99), s.value(10.0), s.value(25.0), s.value(99.0)), (100.0, 40.0, 50.0, 60.0))
        self.assertEqual(s.breakpoints(), [(0.0, 100.0, None), (10.0, 40.0, None), (20.0, 40.0, (30.0, 60.0)),
                                           (30.0, 60.0, None)])
        with self.assertRaises(ValueError):
            Supply(((5.0, 1.0),))

    def test_scenario_is_keyed_and_jittered(self):
        a, b = supply_scenario(CFG, 1000.0, run=0), supply_scenario(CFG, 1000.0, run=0)
        self.assertEqual(a, b)
        shifts = []
        for run in range(5):
            s = supply_scenario(CFG, 1000.0, run=run)
            self.assertEqual(s.points[0], (0.0, 700.0))
            d = {round(t - h * HOUR, 3) for (t, _), (h, _) in zip(s.points[1:], CFG.supply_profile[1:])}
            self.assertEqual(len(d), 1)                               # every point moves by the same draw
            shifts.append(d.pop())
        self.assertTrue(all(abs(x) <= CFG.supply_jitter_h * HOUR for x in shifts))
        self.assertGreater(len(set(shifts)), 1)
        fixed = supply_scenario(CFG.with_(supply_jitter_h=0.0), 1000.0, run=3)
        self.assertEqual(fixed.points[2], (3 * HOUR, 450.0))


class IslandRig:
    """An island with real substation verifiers wired to it, flat loads and a given supply."""

    def __init__(self, supply_points, specs=None, cfg=None, horizon_h=3.0):
        self.cfg = (cfg or CFG).with_(load_noise=0.0, horizon_h=horizon_h)
        self.specs = specs or [
            FeederSpec("S1-F1", "S1", (("telecom", 1000.0),)),
            FeederSpec("S1-F2", "S1", (("residential", 1000.0),), 1),
            FeederSpec("S2-F1", "S2", (("residential", 1000.0),), 2),
            FeederSpec("S2-F2", "S2", (("commercial", 1000.0),), 3)]
        self.kernel, self.log = Kernel(self.cfg.horizon_s), EventLog()
        self.loads = Loads(self.cfg, self.specs)
        self.island = Island(self.cfg, self.kernel, self.log, self.specs, self.loads, Supply(tuple(supply_points)))
        self.subs = {s: Verifier(self.cfg, s, {f: "on" for f in fs}, log=self.log,
                                 interlock_fn=self.island.interlock, on_switch=self.island.switched)
                     for s, fs in by_substation(self.specs).items()}
        self.island.attach(self.subs)

    def run(self, until=None):
        self.island.start(0.0)
        self.kernel.run(until)
        return self

    def at(self, t, fn, *args):
        self.kernel.schedule(t, PRIO_MEASURE, fn, *args)

    def events(self, kind):
        return list(self.log.of_kind(kind))


class TestIsland(unittest.TestCase):
    def test_load_within_supply_trips_nothing(self):
        rig = IslandRig([(0.0, 1e6)]).run()
        self.assertEqual(rig.events(EventKind.UFLS_TRIP), [])
        self.assertEqual(rig.events(EventKind.ISLAND_COLLAPSE), [])
        self.assertEqual(len(rig.events(EventKind.SUPPLY_CHANGED)), 1)

    def test_ufls_trips_stages_in_order_until_the_load_fits(self):
        rig = IslandRig([(0.0, 1e6)])
        load = rig.island.served_kw(HOUR)
        rig.island.supply = Supply(((0.0, 1e6), (HOUR, 1e6), (HOUR, load * 0.7)))     # a strike at 1 h
        rig.run()
        trips = rig.events(EventKind.UFLS_TRIP)
        self.assertEqual([e.data["stage"] for e in trips], [1, 2])
        self.assertTrue(all(abs(e.t - (HOUR + rig.cfg.ufls_delay_s)) < 1e-6 for e in trips))
        self.assertEqual(rig.island.state, {"S1-F1": "on", "S1-F2": "off", "S2-F1": "off", "S2-F2": "on"})
        self.assertEqual(rig.subs["S1"].version, 1)                   # a local action bumps the state version
        self.assertLessEqual(rig.island.served_kw(2 * HOUR), rig.island.supply.value(2 * HOUR) + 1e-6)
        self.assertEqual(rig.events(EventKind.ISLAND_COLLAPSE), [])

    def test_a_short_overload_or_one_within_the_margin_trips_nothing(self):
        rig = IslandRig([(0.0, 1e6)])
        load = rig.island.served_kw(HOUR)
        rig.island.supply = Supply(((0.0, 1e6), (HOUR, 1e6), (HOUR, load / 1.04)))
        rig.run()
        self.assertEqual(rig.events(EventKind.UFLS_TRIP), [])
        rig = IslandRig([(0.0, 1e6)])
        rig.island.supply = Supply(((0.0, 1e6), (HOUR, 1e6), (HOUR, load * 0.5), (HOUR + 1.0, load * 0.5),
                                    (HOUR + 1.0, 1e6)))
        rig.run()
        self.assertEqual(rig.events(EventKind.UFLS_TRIP), [])        # gone before ufls_delay_s

    def test_collapse_when_shedding_every_stage_is_not_enough(self):
        rig = IslandRig([(0.0, 1e6)])
        crit = rig.loads.forecast("S1-F1", HOUR)
        rig.island.supply = Supply(((0.0, 1e6), (HOUR, 1e6), (HOUR, crit * 0.5)))
        rig.run(1.5 * HOUR)
        self.assertEqual(len(rig.events(EventKind.UFLS_TRIP)), 3)
        (c,) = rig.events(EventKind.ISLAND_COLLAPSE)
        t = c.t
        self.assertTrue(all(st == "off" for st in rig.island.state.values()))
        self.assertTrue(rig.island.dark(t + 10.0))
        self.assertEqual(rig.island.measure(int((t + 600.0) // CFG.dispatch_period_s))["supply_kw"], 0.0)
        self.assertEqual(rig.island.served_kw(t + 10.0), 0.0)
        self.assertFalse(rig.subs["S1"].local_action("S1-F1", "on", t + 10.0, "test"))   # nothing closes on a dead bus
        self.assertEqual(c.data["dark_until"], round(t + CFG.blackstart_s, 3))

    def test_dead_bus_interlock_on_a_command_and_black_start(self):
        rig = IslandRig([(0.0, 1e6)], horizon_h=4.0)
        crit = rig.loads.forecast("S1-F1", HOUR)
        rig.island.supply = Supply(((0.0, 1e6), (HOUR, 1e6), (HOUR, crit * 0.5), (1.5 * HOUR, crit * 0.5),
                                    (1.5 * HOUR, 1e6)))
        seen = {}

        def try_close(label):
            v = rig.subs["S1"]
            seen[label] = v._execute(make_command("S1", "S1-F1", "on", v.last_seq + 1, v.version, 1e9),
                                     {"substation": "S1", "height": None, "delivery": label, "from": ""},
                                     rig.kernel.now)

        rig.at(1.2 * HOUR, try_close, "dark")
        rig.at(HOUR + CFG.blackstart_s + 60.0, try_close, "after")
        rig.run()
        self.assertEqual(seen, {"dark": False, "after": True})
        rej = rig.events(EventKind.COMMAND_REJECTED)
        self.assertEqual([e.data["reason"] for e in rej], ["dead_bus"])
        causes = [e.data["cause"] for e in rig.events(EventKind.SUPPLY_CHANGED)]
        self.assertIn("collapse", causes)
        self.assertIn("blackstart", causes)
        self.assertEqual(rig.island.state["S1-F1"], "on")

    def test_cold_load_after_an_outage(self):
        rig = IslandRig([(0.0, 1e6)], horizon_h=2.0)
        rig.at(600.0, rig.subs["S1"].local_action, "S1-F2", "off", 600.0, "test")
        rig.at(600.0 + 2 * CFG.cold_load_decay_s, rig.subs["S1"].local_action, "S1-F2", "on",
               600.0 + 2 * CFG.cold_load_decay_s, "test")
        rig.run(1.9 * HOUR)
        t = 600.0 + 2 * CFG.cold_load_decay_s
        base = rig.loads.demand("S1-F2", t + 1.0)
        self.assertAlmostEqual(rig.island.load_kw("S1-F2", t + 1.0) / base,
                               cold_factor(rig.cfg, 2 * CFG.cold_load_decay_s, 1.0), places=6)
        self.assertEqual(rig.island.load_kw("S1-F2", t + CFG.cold_load_decay_s),
                         rig.loads.demand("S1-F2", t + CFG.cold_load_decay_s))


class TestVerifierBookkeeping(unittest.TestCase):
    def test_report_tracks_switches_by_the_local_clock(self):
        v = Verifier(CFG, "S1", {"S1-F1": "on", "S1-F2": "on"}, clock_offset_s=100.0)
        self.assertTrue(v.local_action("S1-F2", "off", 1000.0, "ufls"))
        self.assertFalse(v.local_action("S1-F2", "off", 1001.0, "ufls"))          # no change, no version
        self.assertTrue(v.local_action("S1-F2", "on", 1600.0, "table"))
        self.assertTrue(v.local_action("S1-F2", "off", 2000.0, "ufls"))
        r = v.report()
        self.assertEqual((r["version"], r["last_seq"]), (3, 0))
        self.assertEqual(r["states"], {"S1-F1": "on", "S1-F2": "off"})
        self.assertEqual(r["since"], {"S1-F1": None, "S1-F2": 2100.0})
        self.assertEqual((r["off_closed"]["S1-F2"], r["last_off"]["S1-F2"]), (600.0, 600.0))
        causes = [e.data["cause"] for e in v.log.of_kind(EventKind.FEEDER_SWITCHED)]
        self.assertEqual(causes, ["ufls", "table", "ufls"])
        self.assertTrue(math.isclose(v.local_time(0.0), 100.0))


if __name__ == "__main__":
    unittest.main()
