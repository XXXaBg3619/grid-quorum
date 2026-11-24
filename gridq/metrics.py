"""Metrics. Helpers ported from evsim/metrics.py (BlokcChin@796b43c): jain,
mean_ci95 and the paired difference; the grid metrics are still to be written.

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

import math
import statistics
from typing import Iterable, List, Tuple


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
