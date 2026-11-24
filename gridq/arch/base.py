"""Architecture interface: A, B, C and D implement exactly this (pattern ported
from evsim/mechanisms/base.py, BlokcChin@796b43c).

Design principle carried over from evsim: one engine, one policy, one set of
scenarios; the architectures differ only in how a decision reaches a
substation. A difference in the results then comes from the decision path, not
from a different rule or workload.

The simulator calls:
  start()             once, before the first dispatch tick
  on_dispatch(k, t)   at dispatch tick k: decide target feeder states through
                      this architecture's path and send the commands
  on_message(msg)     a network message addressed to this architecture arrived
  finish()            end of the run (counters into log.run_meta)

Architectures ask the fault (sim.fault) at their decision points, log what they
do through sim.emit(), and never read events with visibility "truth".
Signatures may still change when sim.py and consensus/ are written.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Architecture(ABC):
    name = "base"
    decision_path = ""        # who decides and who checks, for reports and the architecture diagram

    def __init__(self, sim):
        self.sim = sim
        self.cfg = sim.cfg

    def start(self) -> None:
        pass

    @abstractmethod
    def on_dispatch(self, k: int, t: float) -> None:
        ...

    def on_message(self, msg) -> None:
        pass

    def finish(self) -> None:
        pass
