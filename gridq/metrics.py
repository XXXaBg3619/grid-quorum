"""Metrics. Helpers ported from evsim/metrics.py (BlokcChin@796b43c): jain,
mean_ci95 and the paired difference. Grid metrics: what substations did with
injected deliveries, committed commands executed, and feeder states against a
reference run (experiment 4); and for a whole island event (island_metrics):
critical-load hours, served energy, UFLS trips, collapses, availability,
commands and rotation fairness.

Principle kept from evsim: metrics are computed from the event log only
(public and truth events), never from an architecture's internal state. That
keeps them independent of the architecture and proves the log carries enough
information.

The island metrics need the loads, which the log does not list: they are
rebuilt from run_meta (config, run, island table, load scale), as the keyed
random draws make them reproducible.

Still to come (design doc, section 下一步：實作範圍與順序):
  conflicting_commits   two different blocks committed at one height (safety)
  commit_latency_s      proposal to commit (C, D); issue to execution at the
                        substation (all architectures)
  break_difficulty      P(more than f validators compromised), C vs D (theory.py)
  data_exposure         sensitive data one compromised node can read
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter
from typing import Dict, Iterable, List, Mapping, Tuple

from .config import GridConfig
from .events import EventKind
from .loads import Loads, specs_from_meta


def jain(xs: Iterable[float]) -> float:
    """Jain's fairness index, 1 = perfectly even (e.g. rotation among non-critical feeders)."""
    xs = list(xs)
    if not xs:
        return 1.0
    s, q = sum(xs), sum(x * x for x in xs)
    return (s * s) / (len(xs) * q) if q > 0 else 1.0


def paired_diff(faulty: dict, honest: dict, keys: Iterable[str],
                higher_is_better: Iterable[str] = ()) -> dict:
    """Paired damage: faulty run minus honest run with the same seed (common random numbers).

    For keys where higher is better (availability, critical-load hours) the sign
    is flipped, so a positive value always means damage (evsim's loss())."""
    hb = set(higher_is_better)
    out = {}
    for k in keys:
        d = faulty[k] - honest[k]
        out[k] = round(-d if k in hb else d, 6) + 0.0
    return out


_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 20: 2.086, 30: 2.042}


def mean_ci95(xs: List[float]) -> Tuple[float, float]:
    """Mean and half-width of the 95% t confidence interval (0 for a single value)."""
    n = len(xs)
    m = statistics.fmean(xs)
    if n < 2:
        return m, 0.0
    df = n - 1
    t = _T95.get(df) or _T95[min(_T95, key=lambda k: abs(k - df))]
    return m, t * statistics.stdev(xs) / math.sqrt(n)


# ---------- the last hop (experiment 4) ----------
def injected_outcomes(log) -> Dict[str, int]:
    """Commands in deliveries an attacker injected, and what the substations did with them.

    Injected deliveries come from truth FAULT_ACTION events that name a
    delivery and its commands; the outcomes from public COMMAND_ACCEPTED and
    COMMAND_REJECTED events with that delivery id. "ignored" are commands of a
    delivery the substation dropped without an event (a copy of its trusted
    header). bad_accept_rate = accepted / commands."""
    injected: Dict[str, int] = {}
    for e in log.of_kind(EventKind.FAULT_ACTION, visibility="truth"):
        if e.data.get("delivery"):
            injected[e.data["delivery"]] = len(e.data.get("commands") or ())
    out: Counter = Counter(deliveries=len(injected), commands=sum(injected.values()), accepted=0, switched=0)
    for e in log.of_kind(EventKind.COMMAND_ACCEPTED):
        if e.data.get("delivery") in injected:
            out["accepted"] += 1
            out["switched"] += bool(e.data.get("switched"))
    for e in log.of_kind(EventKind.COMMAND_REJECTED):
        if e.data.get("delivery") in injected:
            out["rejected:" + str(e.data.get("reason"))] += 1
    out["ignored"] = out["commands"] - out["accepted"] - sum(v for k, v in out.items() if k.startswith("rejected:"))
    return dict(out)


def commands_executed(log) -> Tuple[int, int]:
    """(committed commands, those a substation executed from a delivery nobody injected)."""
    injected = {e.data["delivery"] for e in log.of_kind(EventKind.FAULT_ACTION, visibility="truth")
                if e.data.get("delivery")}
    issued = {(e.data["substation"], e.data["seq"]) for e in log.of_kind(EventKind.COMMAND_ISSUED)}
    done = {(e.data["substation"], e.data["seq"]) for e in log.of_kind(EventKind.COMMAND_ACCEPTED)
            if e.data.get("delivery") not in injected}
    return len(issued), len(issued & done)


def feeder_timelines(log, initial: Mapping[str, Mapping[str, str]]) -> Dict[Tuple[str, str], List[Tuple[float, str]]]:
    """Each feeder's state over time, [(t, state)] from t = 0, from FEEDER_SWITCHED events."""
    out = {(s, f): [(0.0, st)] for s, fs in initial.items() for f, st in fs.items()}
    for e in log.of_kind(EventKind.FEEDER_SWITCHED):
        out[(e.data["substation"], e.data["feeder"])].append((e.t, e.data["state"]))
    return out


def state_deviation_s(actual: Mapping, reference: Mapping, end: float) -> Dict[str, float]:
    """Feeder-seconds in [0, end) in which a feeder's state differs from the reference run's.

    unsafe_on: on where the reference is off (load the decision did not plan
    for, the dangerous side in an island short of power); wrong_off: off where
    the reference is on (customers cut for nothing)."""
    out = {"unsafe_on": 0.0, "wrong_off": 0.0}
    for key, ref in reference.items():
        act = actual.get(key, ref[:1])
        rt, at = [x for x, _ in ref], [x for x, _ in act]
        cuts = sorted({x for x in rt + at if 0.0 < x < end} | {0.0, end})
        for a, b in zip(cuts, cuts[1:]):
            sa = act[bisect.bisect_right(at, a) - 1][1]
            sr = ref[bisect.bisect_right(rt, a) - 1][1]
            if sa != sr:
                out["unsafe_on" if sa == "on" else "wrong_off"] += b - a
    return out


# ---------- a whole island event ----------
def config_from_meta(d: Mapping) -> GridConfig:
    """The GridConfig a run's log was made with (run_meta["config"])."""
    kw = {k: v for k, v in d.items() if k not in ("f", "quorum")}
    kw["supply_profile"] = tuple(tuple(p) for p in kw["supply_profile"])
    return GridConfig(**kw)


def on_intervals(timeline: List[Tuple[float, str]], end: float) -> List[Tuple[float, float]]:
    """[(start, stop)] in [0, end) when a feeder was on, from its [(t, state)] timeline."""
    out, since = [], None
    for t, st in timeline + [(end, "end")]:
        t = min(t, end)
        if st == "on" and since is None:
            since = t
        elif st != "on" and since is not None:
            if t > since:
                out.append((since, t))
            since = None
    return out


def island_metrics(log) -> dict:
    """One island event's results, from its log (public events) and run_meta.

      critical_served_share  feeder time the critical feeders were on, over all
                             their time (critical_hours_lost: the rest, hours)
      served_share           customer energy served over customer energy
                             demanded (actual demand without cold-load pickup)
      ufls_trips, ufls_events  UFLS stage trips, and distinct moments with one
      collapses, dark_h      island collapses and hours dark
      availability           share of (substation, dispatch slot) pairs in
                             which the substation verified a fresh decision for
                             the slot before the slot ended
      decided_share          slots with a decision (DECISION, any issuer)
      issued, executed       committed or centre-issued commands, and those
                             executed (exactly once each, by (substation, seq))
      rejected:<reason>      COMMAND_REJECTED by reason
      fairness               Jain's index of the non-critical feeders' on-time
      failovers, fallback_starts, table_switches  A and B's centre take-overs,
                             B's substations entering the pre-signed table, and
                             feeders it switched
    """
    meta = log.run_meta
    cfg = config_from_meta(meta["config"])
    specs = specs_from_meta(meta["island"])
    end, period = cfg.horizon_s, cfg.dispatch_period_s
    n_slots = cfg.n_dispatches
    initial: Dict[str, Dict[str, str]] = {}
    for sp in specs:
        initial.setdefault(sp.substation, {})[sp.name] = "on"
    tl = feeder_timelines(log, initial)
    on = {sp.name: on_intervals(tl[(sp.substation, sp.name)], end) for sp in specs}
    on_s = {f: sum(b - a for a, b in iv) for f, iv in on.items()}
    crit = [sp.name for sp in specs if sp.critical]
    rest = [sp.name for sp in specs if not sp.critical]

    loads = Loads(cfg, specs, meta.get("run", 0), meta.get("load_scale", 1.0))
    step = 60.0
    grid = [(i + 0.5) * step for i in range(int(end // step))]
    served = demand = 0.0
    for sp in specs:
        iv, j = on[sp.name], 0
        for t in grid:
            d = loads.demand(sp.name, t) * step / 3600.0
            demand += d
            while j < len(iv) and iv[j][1] <= t:
                j += 1
            if j < len(iv) and iv[j][0] <= t:
                served += d

    ufls = list(log.of_kind(EventKind.UFLS_TRIP))
    collapses = list(log.of_kind(EventKind.ISLAND_COLLAPSE))
    applied = {(e.data.get("substation"), e.data.get("slot")) for e in log.of_kind(EventKind.DECISION_APPLIED)
               if type(e.data.get("slot")) is int and e.t < (e.data["slot"] + 1) * period}
    decided = {e.data.get("slot") for e in log.of_kind(EventKind.DECISION)}
    issued, executed = commands_executed(log)
    out = {
        "critical_served_share": round(sum(on_s[f] for f in crit) / (len(crit) * end), 6) if crit else 1.0,
        "critical_hours_lost": round(sum(end - on_s[f] for f in crit) / 3600.0, 4),
        "served_share": round(served / demand, 6) if demand else 1.0,
        "served_mwh": round(served / 1000.0, 3), "demand_mwh": round(demand / 1000.0, 3),
        "ufls_trips": len(ufls), "ufls_events": len({round(e.t, 6) for e in ufls}),
        "ufls_feeders": sum(len(e.data.get("feeders", ())) for e in ufls),
        "collapses": len(collapses),
        "dark_h": round(sum(max(0.0, min(e.data["dark_until"], end) - e.t) for e in collapses) / 3600.0, 4),
        "availability": round(len(applied) / (len(initial) * n_slots), 6) if n_slots else 1.0,
        "decided_share": round(len(decided & set(range(n_slots))) / n_slots, 6) if n_slots else 1.0,
        "issued": issued, "executed": executed,
        "fairness": round(jain(on_s[f] for f in rest), 6),
        "failovers": sum(1 for e in log.of_kind(EventKind.FAILOVER) if e.data.get("action") == "take_over"),
        "fallback_starts": sum(1 for e in log.of_kind(EventKind.FALLBACK_SCHEDULE) if e.data.get("action") == "start"),
        "table_switches": sum(1 for e in log.of_kind(EventKind.FEEDER_SWITCHED) if e.data.get("cause") == "table"),
    }
    for reason, n in sorted(Counter(e.data.get("reason") for e in log.of_kind(EventKind.COMMAND_REJECTED)).items()):
        out[f"rejected:{reason}"] = n
    return out
