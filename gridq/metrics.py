"""Metrics. Helpers ported from evsim/metrics.py (BlokcChin@796b43c): jain,
mean_ci95 and the paired difference. Grid metrics so far: what substations did
with injected deliveries, committed commands executed, and feeder states
against a reference run (experiment 4); the rest is still to be written.

Principle kept from evsim: metrics are computed from the event log only
(public and truth events), never from an architecture's internal state. That
keeps them independent of the architecture and proves the log carries enough
information.

Planned metrics (design doc, section 下一步：實作範圍與順序):
  conflicting_commits   two different blocks committed at one height (safety)
  bad_accept_rate       share of violating, stale, replayed or under-signed
                        commands that a substation executed
  commit_latency_s      proposal to commit (C, D); issue to execution at the
                        substation (all architectures)
  availability          share of dispatch periods in which a new decision
                        could still take effect
  critical_load_hours   hours the critical feeders (hospital, water, telecom,
                        shelter) were served
  island_collapses      ISLAND_COLLAPSE events
  break_difficulty      P(more than f validators compromised), C vs D (theory.py)
  data_exposure         sensitive data one compromised node can read
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter
from typing import Dict, Iterable, List, Mapping, Tuple

from .events import EventKind


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
