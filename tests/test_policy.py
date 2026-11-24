"""Allocation policy (gridq/policy.py): the rule, commands, bookkeeping, B's local check and
table, the global check, and C and D's chain dispatch (lasthop.PolicyDispatch)."""
import os
import random
import sys
import unittest
from collections import Counter
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.config import GridConfig  # noqa: E402
from gridq.crypto import KeyRing  # noqa: E402
from gridq.lasthop import PolicyDispatch  # noqa: E402
from gridq.loads import Loads, by_substation, default_island  # noqa: E402
from gridq.policy import (LocalEnforcer, advance_view, allocate, check_global, check_local, commands_for,  # noqa: E402
                          decide, feeder_inputs, planned_kw, start_view, static_table)
from gridq.substation import report_digest  # noqa: E402
from gridq.verifier import Verifier  # noqa: E402

CFG = GridConfig()
FLAT = CFG.with_(cold_load_multiplier=1.0)        # no cold-load pickup: the ordering on its own
SPECS = default_island()
SUBS = sorted(by_substation(SPECS))


def x(state="off", critical=False, weight=2.0, off_s=0.0, on_s=0.0, off_total=None, demand=100.0, last_off=0.0):
    """One feeder's allocate() input."""
    return {"critical": critical, "weight": weight, "state": state, "off_s": off_s, "on_s": on_s,
            "last_off": last_off, "off_total": off_s if off_total is None else off_total, "demand_kw": demand}


def budget_for(kw):
    return kw / (1.0 - CFG.reserve_margin)


def measurement(supply, slot=0, loads=None):
    loads = loads or Loads(CFG, SPECS)
    return {"supply_kw": supply, "demand_kw": {s.name: loads.slot_forecast(s.name, slot * CFG.dispatch_period_s)
                                               for s in SPECS}}


class TestAllocate(unittest.TestCase):
    def test_critical_first_whatever_the_urgency(self):
        fs = {"crit": x("off", critical=True, weight=70.0, off_s=0.0, demand=100.0),
              "res": x("off", weight=2.0, off_s=9e5, demand=100.0)}
        self.assertEqual(allocate(FLAT, fs, budget_for(100.0)), {"crit": "on", "res": "off"})
        self.assertEqual(allocate(FLAT, fs, budget_for(200.0)), {"crit": "on", "res": "on"})

    def test_critical_feeders_by_weight_when_short(self):
        fs = {"hosp": x("on", critical=True, weight=100.0, on_s=1e9), "shelter": x("on", critical=True, weight=70.0,
                                                                                   on_s=1e9)}
        self.assertEqual(allocate(CFG, fs, budget_for(150.0)), {"hosp": "on", "shelter": "off"})

    def test_rotation_by_accumulated_outage(self):
        """(With cold load, an outage of an hour doubles a feeder's planned load: see the next tests.)"""
        fs = {"a": x("off", off_s=3600.0, off_total=3600.0), "b": x("off", off_s=3600.0, off_total=7200.0),
              "c": x("on", on_s=7200.0)}
        out = allocate(FLAT, fs, budget_for(100.0))
        self.assertEqual(out, {"a": "off", "b": "on", "c": "off"})    # c is past its dwell, b waited longest
        for w, expect in ((3.0, "off"), (4.0, "on"), (5.0, "on")):          # urgency = weight x outage
            heavier = dict(fs, a=x("off", weight=w, off_s=3600.0, off_total=3600.0))
            self.assertEqual(allocate(FLAT, heavier, budget_for(100.0))["a"], expect, w)   # 4: a tie, weight first

    def test_dwell_keeps_new_states_unless_the_budget_forces(self):
        fs = {"new_on": x("on", on_s=600.0), "old_off": x("off", off_s=7200.0), "new_off": x("off", off_s=600.0,
                                                                                            off_total=1e6)}
        self.assertEqual(allocate(FLAT, fs, budget_for(100.0)), {"new_on": "on", "old_off": "off", "new_off": "off"})
        self.assertEqual(allocate(FLAT, fs, budget_for(200.0)), {"new_on": "on", "old_off": "on", "new_off": "off"})
        self.assertEqual(allocate(FLAT, fs, budget_for(300.0))["new_off"], "on")      # last, if anything is left
        self.assertEqual(allocate(FLAT, fs, 0.0), {"new_on": "off", "old_off": "off", "new_off": "off"})

    def test_cold_load_keeps_a_warm_feeder_on_instead_of_swapping(self):
        fs = {"b": x("off", off_s=3600.0, off_total=7200.0), "c": x("on", on_s=7200.0)}
        self.assertEqual(allocate(CFG, fs, budget_for(100.0)), {"b": "off", "c": "on"})   # b would need 200
        self.assertEqual(allocate(FLAT, fs, budget_for(100.0)), {"b": "on", "c": "off"})

    def test_cold_load_is_planned(self):
        long_off = x("off", off_s=2 * CFG.cold_load_decay_s, demand=100.0)
        self.assertAlmostEqual(planned_kw(CFG, long_off), 100.0 * CFG.cold_load_multiplier)
        self.assertEqual(allocate(CFG, {"f": long_off}, budget_for(150.0)), {"f": "off"})
        self.assertEqual(allocate(CFG, {"f": long_off}, budget_for(200.0)), {"f": "on"})
        warm = x("on", on_s=CFG.cold_load_decay_s / 2, last_off=2 * CFG.cold_load_decay_s, demand=100.0)
        self.assertAlmostEqual(planned_kw(CFG, warm), 100.0 * (1 + (CFG.cold_load_multiplier - 1) / 2))

    def test_never_over_budget_deterministic_and_order_free(self):
        rng = random.Random(7)
        for _ in range(200):
            fs = {f"f{i}": x(rng.choice(("on", "off")), critical=rng.random() < 0.2, weight=rng.choice((1, 2, 70, 100)),
                             off_s=rng.choice((0.0, 600.0, 4000.0)), on_s=rng.choice((300.0, 2400.0, 1e9)),
                             off_total=rng.uniform(0, 1e4), demand=rng.uniform(50, 400),
                             last_off=rng.uniform(0, 4000)) for i in range(8)}
            supply = rng.uniform(0, 2500)
            out = allocate(CFG, fs, supply)
            shuffled = dict(rng.sample(list(fs.items()), len(fs)))
            self.assertEqual(allocate(CFG, shuffled, supply), out)
            planned = sum(planned_kw(CFG, fs[f]) for f in fs if out[f] == "on")
            self.assertLessEqual(planned, supply * (1 - CFG.reserve_margin) + 1e-6)

    def test_durations_compare_in_whole_minutes(self):
        """A command executed a fraction of a second later must not reorder two feeders."""
        view = start_view(SPECS)
        for s, f, t in (("S1", "S1-F2", 300.07), ("S2", "S2-F2", 300.31)):
            view[s] = {**view[s], "states": {**view[s]["states"], f: "off"}, "since": {**view[s]["since"], f: t}}
        xs = feeder_inputs(CFG, SPECS, view, measurement(1e6, 12), 12)
        self.assertEqual(xs["S1-F2"]["off_total"], xs["S2-F2"]["off_total"])
        self.assertEqual(xs["S1-F2"]["off_s"], 3300.0)


class TestCommandsAndBookkeeping(unittest.TestCase):
    def test_commands_are_absolute_numbered_and_versioned(self):
        view = start_view(SPECS)
        targets = {s.name: "on" for s in SPECS}
        targets.update({"S1-F2": "off", "S1-F3": "off", "S3-F2": "off"})
        cmds = commands_for(CFG, targets, view, {"S1": 7}, slot=4)
        self.assertEqual([(c["substation"], c["feeder"], c["target"], c["seq"], c["state_version"]) for c in cmds],
                         [("S1", "S1-F2", "off", 8, 0), ("S1", "S1-F3", "off", 9, 1), ("S3", "S3-F2", "off", 1, 0)])
        self.assertEqual({c["expires"] for c in cmds}, {4 * CFG.dispatch_period_s + CFG.command_ttl_s})
        self.assertEqual(commands_for(CFG, {s.name: "on" for s in SPECS}, view, {}, 0), [])

    def test_advance_view_books_like_a_verifier(self):
        feeders = by_substation(SPECS)
        vs = {s: Verifier(CFG, s, {f: "on" for f in fs}) for s, fs in feeders.items()}
        view = start_view(SPECS)
        rng = random.Random(3)
        for k in range(1, 30):
            t = k * CFG.dispatch_period_s
            targets = {sp.name: rng.choice(("on", "off")) for sp in SPECS}
            view = advance_view(view, targets, t)
            for s, v in vs.items():
                for f in feeders[s]:
                    v.local_action(f, targets[f], t, "test")
        for s, v in vs.items():
            r = v.report()
            mine = view[s]
            self.assertEqual((mine["version"], mine["states"], mine["since"]), (r["version"], r["states"], r["since"]))
            for key in ("off_closed", "last_off"):
                self.assertEqual({f: round(y, 3) for f, y in mine[key].items()}, r[key])


class TestLocalCheck(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(check_local(True, "on", "off", 0, CFG), "critical_feeder")
        self.assertEqual(check_local(True, "off", "on", 0, CFG), "")
        self.assertEqual(check_local(False, "on", "off", CFG.local_max_switch, CFG), "too_many_switchings")
        self.assertEqual(check_local(True, "on", "on", 99, CFG), "")            # no switching, nothing to check

    def test_enforcer_counts_per_dispatch_period(self):
        enf = LocalEnforcer(CFG, SPECS)
        v = Verifier(CFG, "S1", {"S1-F1": "on", "S1-F2": "on", "S1-F3": "on"})
        c = lambda f, tgt: {"substation": "S1", "feeder": f, "target": tgt}  # noqa: E731
        self.assertFalse(enf(v, c("S1-F1", "off"), 10.0))                     # the hospital feeder
        self.assertTrue(enf(v, c("S1-F2", "off"), 10.0))
        self.assertTrue(enf(v, c("S1-F3", "off"), 11.0))
        v.feeders.update({"S1-F2": "off", "S1-F3": "off"})
        self.assertFalse(enf(v, c("S1-F2", "on"), 12.0))                      # a third switching this period
        self.assertTrue(enf(v, c("S1-F2", "on"), CFG.dispatch_period_s + 1.0))   # next period
        self.assertEqual(enf.refused, {"critical_feeder": 1, "too_many_switchings": 1})


class TestGlobalCheckAndTable(unittest.TestCase):
    def test_global_check(self):
        view, slot = start_view(SPECS), 40
        meas = measurement(0.45 * Loads(CFG, SPECS).peak_kw(), slot)
        self.assertEqual(check_global(CFG, SPECS, view, meas, slot, []), "overload")    # all on at night + strike
        targets = decide(CFG, SPECS, view, meas, slot)
        self.assertEqual(check_global(CFG, SPECS, view, meas, slot, commands_for(CFG, targets, view, {}, slot)), "")
        cut = commands_for(CFG, dict(targets, **{"S1-F1": "off"}), view, {}, slot)
        self.assertEqual(check_global(CFG, SPECS, view, meas, slot, cut), "critical_off")
        restore_all = commands_for(CFG, {s.name: "on" for s in SPECS}, advance_view(view, targets, 0.0), {}, slot)
        self.assertEqual(check_global(CFG, SPECS, advance_view(view, targets, 0.0), meas, slot, restore_all),
                         "overload")

    def test_decide_always_passes_the_global_check(self):
        rng, loads = random.Random(11), Loads(CFG, SPECS)
        peak = loads.peak_kw()
        view = start_view(SPECS)
        for slot in range(0, 288, 7):
            meas = measurement(rng.uniform(0.2, 0.8) * peak, slot, loads)
            targets = decide(CFG, SPECS, view, meas, slot)
            cmds = commands_for(CFG, targets, view, {}, slot)
            self.assertEqual(check_global(CFG, SPECS, view, meas, slot, cmds), "", slot)
            view = advance_view(view, {f: rng.choice(("on", "off")) if rng.random() < 0.3 else st
                                       for f, st in targets.items()}, slot * CFG.dispatch_period_s)

    def test_static_table(self):
        loads = Loads(CFG, SPECS)
        plan = CFG.plan_supply_share * loads.peak_kw()
        table = static_table(CFG, SPECS, loads, plan, 288)
        self.assertEqual(len(table), 288)
        view = start_view(SPECS)
        for k, row in enumerate(table):
            self.assertTrue(all(row[s.name] == "on" for s in SPECS if s.critical), k)
            meas = measurement(plan, k, loads)
            self.assertEqual(check_global(CFG, SPECS, view, meas, k, commands_for(CFG, row, view, {}, k)), "", k)
            view = advance_view(view, row, k * CFG.dispatch_period_s)
        offs = Counter(f for row in table for f, st in row.items() if st == "off")
        self.assertGreater(len(offs), 4)                                      # the table rotates


class TestPolicyDispatch(unittest.TestCase):
    """C and D: every validator recomputes the commands from the reports and measurements in a block."""

    def setUp(self):
        self.keys = KeyRing(SUBS, CFG.seed)
        self.loads = Loads(CFG, SPECS)
        self.supply = 0.45 * self.loads.peak_kw()
        self.d = PolicyDispatch(CFG, SPECS, self.keys, lambda k: measurement(self.supply, k, self.loads))
        self.node = SimpleNamespace(name="stub", chain=[])

    def report(self, sub, keys=None, **change):
        feeders = by_substation(SPECS)[sub]
        v = Verifier(CFG, sub, {f: "on" for f in feeders})
        for f, st in change.items():
            v.local_action(f.replace("_", "-"), st, 100.0, "ufls")
        r = v.report()
        r["sig"] = (keys or self.keys).sign(sub, report_digest(CFG, r))
        return r

    def block(self, reports, slot=40, measurements=None):
        meas = measurements or self.d.extras(slot)["measurements"]
        view = self.d._updated(self.d.start_state(), reports)
        return {"slot": slot, "payload": {"reports": reports, "measurements": meas,
                                          "commands": self.d.block_commands(view, Counter(), slot,
                                                                           {"measurements": meas})}}

    def test_valid_block_and_what_breaks_it(self):
        good = self.block([])
        self.assertTrue(good["payload"]["commands"])                        # the strike: shed feeders
        self.assertTrue(self.d.valid(self.node, good))
        p = good["payload"]
        for payload in ({**p, "commands": p["commands"][1:]},
                        {**p, "measurements": {**p["measurements"], "supply_kw": p["measurements"]["supply_kw"] * 2}},
                        {k: v for k, v in p.items() if k != "measurements"}):
            self.assertFalse(self.d.valid(self.node, {"slot": 40, "payload": payload}))

    def test_reports_change_the_commands_and_must_be_full_and_signed(self):
        r = self.report("S1", S1_F2="off")
        ok = self.block([r])
        self.assertTrue(self.d.valid(self.node, ok))
        self.assertNotIn("S1-F2", [c["feeder"] for c in ok["payload"]["commands"] if c["target"] == "off"])
        forged = self.report("S1", keys=KeyRing(SUBS, seed=99), S1_F2="off")
        self.assertFalse(self.d.valid(self.node, self.block([forged])))
        thin = {k: r[k] for k in ("substation", "version", "states")}
        thin["sig"] = self.keys.sign("S1", report_digest(CFG, thin))
        self.assertFalse(self.d.authentic(thin))                            # policy needs the bookkeeping


if __name__ == "__main__":
    unittest.main()
