"""Architecture interface: A, B, C and D implement exactly this (pattern ported
from evsim/mechanisms/base.py, BlokcChin@796b43c).

Design principle carried over from evsim: one engine, one policy, one set of
scenarios; the architectures differ only in how a decision reaches a
substation. A difference in the results then comes from the decision path, not
from a different rule or workload.

An architecture builds its decision path (`link`) from the simulator's parts
(sim.cfg, kernel, net, log, fault, specs, measure, loads, peak_kw, checks,
clock_offset_s, verifier_kw()):

  A, B   central.CentralPath: a dispatch centre and its backup decide with
         policy.decide and sign each command; B adds the substation enforcer
         (policy.LocalEnforcer) and the pre-signed rotation table
  C, D   lasthop.LastHop with lasthop.PolicyDispatch: the committee decides
         the same policy on the reports and measurements in each block, and
         substations check the commit proof

The simulator calls start(t) once and finish() at the end. The path is also
the `sim` that fault hooks receive, and link.subs are the substations the
island switches through. `orgs` maps every decision node to its organisation
(C: one, D: seven), which the common-cause model of experiment 6 will use.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict


class Architecture(ABC):
    name = "base"
    decision_path = ""        # who decides and who checks, for reports and the architecture diagram

    def __init__(self, sim):
        self.sim = sim
        self.cfg = sim.cfg
        self.link = self.build()

    @abstractmethod
    def build(self):
        """The decision path: central.CentralPath (A, B) or lasthop.LastHop (C, D)."""

    @property
    def substations(self):
        return self.link.subs

    @property
    def orgs(self) -> Dict[str, str]:
        return {}

    def start(self, t: float = 0.0) -> None:
        self.link.start(t)

    def finish(self) -> None:
        """Counters into the log's run_meta."""
        self.sim.log.run_meta["arch"] = {"name": self.name, "decision_path": self.decision_path, "orgs": self.orgs,
                                         "link_stats": dict(sorted(self.link.stats.items()))}
