"""Fault injection for the six experiments (hook pattern ported from
evsim/attacks.py, BlokcChin@796b43c).

As in evsim, one Fault object works for every architecture: each architecture
calls only the hooks that exist in its design, and every hook defaults to
honest behaviour. So exactly the same fault runs against A, B, C and D.

Hooks (signatures may still change when consensus/ and sim.py are written):
  begin_run(sim, rng)                        before a run; schedule timed actions
                                             (partitions via sim.net, replays, crashes)
  node_up(sim, node, t) -> bool              crash or offline                        exp 2
  proposal(sim, node, height, rnd, block, recipients) -> {recipient: block}
                                             different blocks to different
                                             recipients = equivocation               exp 1
  vote(sim, node, kind, height, rnd, value, recipients) -> {recipient: value or None}
                                             byzantine or withheld votes             exp 1
  central_commands(sim, commands) -> commands  a hacked dispatch centre (A, B)        exp 5
  compromised(sim) -> set of nodes           whose keys the attacker holds           exp 6

The six faults (design doc table): 1 an equivocating validator; 2 one to three
validators offline; 3 partition or delay; 4 replay of an executed command;
5 restore commands that are each valid but overload the island together;
6 common-cause compromise (several nodes of one organisation at once).
Only Honest exists so far.
"""
from __future__ import annotations

import random
from typing import Dict, Iterable, List, Set


class Fault:
    label = "honest"

    def __init__(self, p_run: float = 1.0):
        self.p_run = p_run          # share of runs in which the fault is active (evsim's p_night)
        self.active = False

    def describe(self) -> dict:
        return {"label": self.label, "p_run": self.p_run}

    def begin_run(self, sim, rng: random.Random) -> None:
        self.active = self.p_run > 0.0 and (self.p_run >= 1.0 or rng.random() < self.p_run)

    # ---- hooks (default: honest) ----
    def node_up(self, sim, node: str, t: float) -> bool:
        return True

    def proposal(self, sim, node: str, height: int, rnd: int, block,
                 recipients: Iterable[str]) -> Dict[str, object]:
        return {r: block for r in recipients}

    def vote(self, sim, node: str, kind: str, height: int, rnd: int, value,
             recipients: Iterable[str]) -> Dict[str, object]:
        return {r: value for r in recipients}

    def central_commands(self, sim, commands: List) -> List:
        return commands

    def compromised(self, sim) -> Set[str]:
        return set()


class Honest(Fault):
    def __init__(self):
        super().__init__(0.0)


def build(name: str, **kw) -> Fault:
    """Fault by name; the six experiment faults are added here as they are written."""
    if name.upper() in ("NONE", "HONEST"):
        return Honest()
    raise ValueError(f"unknown or not yet implemented fault {name!r}")
