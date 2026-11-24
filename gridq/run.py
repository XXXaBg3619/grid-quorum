"""Run one configuration over many runs, paired with its honest baseline.

Pattern from evsim/run.py (BlokcChin@796b43c):
  * separate RNG streams for loads, supply, network and faults, keyed by the
    seed and the run, so the honest and the faulty run of a pair see identical
    loads, supply and network (common random numbers) and the variance of the
    difference stays small;
  * run_paired(cfg, fault) runs both for every run and returns per-run
    metrics, the paired damage (metrics.paired_diff: positive is worse) and
    means with 95 % confidence intervals over the runs (metrics.mean_ci95);
  * the same pairing holds across architectures: A, B, C and D get the same
    loads, supply scenario, network and fault, so only the decision path
    differs.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Union

from .faults import Fault, Honest
from .metrics import island_metrics, mean_ci95, paired_diff
from .sim import Simulator, simulate

# island_metrics keys where higher is better; paired_diff flips their sign
HIGHER_IS_BETTER = ("critical_served_share", "served_share", "availability", "decided_share", "fairness",
                    "executed", "served_mwh")
DAMAGE_KEYS = ("critical_served_share", "critical_hours_lost", "served_share", "ufls_trips", "ufls_events",
               "collapses", "dark_h", "availability", "decided_share", "fairness")


def summarize(sim_or_log) -> dict:
    """island_metrics of a finished run (a Simulator or its log)."""
    log = sim_or_log.log if isinstance(sim_or_log, Simulator) else sim_or_log
    return island_metrics(log)


def run_paired(cfg, fault: Union[Fault, Callable[[int], Fault], None], runs: Optional[int] = None,
               **kw) -> Dict[str, object]:
    """For run = 0 .. runs - 1 (default cfg.runs): the honest run and the faulty run with the same seed.

    fault: a Fault, or a function run -> Fault when the fault depends on the run
    (e.g. sites lost at that run's strike time). Returns {"honest": [metrics],
    "faulty": [metrics], "damage": [paired diff], "mean": {key: (mean, ci95)}}
    where damage is positive when the fault made things worse."""
    runs = cfg.runs if runs is None else runs
    honest: List[dict] = []
    faulty: List[dict] = []
    damage: List[dict] = []
    for r in range(runs):
        h = summarize(simulate(cfg, Honest(), r, **kw))
        f = fault(r) if callable(fault) and not isinstance(fault, Fault) else fault
        x = summarize(simulate(cfg, f, r, **kw))
        honest.append(h)
        faulty.append(x)
        damage.append(paired_diff(x, h, DAMAGE_KEYS, HIGHER_IS_BETTER))
    mean = {k: tuple(round(v, 6) for v in mean_ci95([d[k] for d in damage])) for k in DAMAGE_KEYS}
    return {"honest": honest, "faulty": faulty, "damage": damage, "mean": mean}
