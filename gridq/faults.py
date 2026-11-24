"""Fault injection for the six experiments (hook pattern ported from
evsim/attacks.py, BlokcChin@796b43c).

As in evsim, one Fault object works for every architecture: each architecture
calls only the hooks that exist in its design, and every hook defaults to
honest behaviour. So exactly the same fault runs against A, B, C and D.

Hooks (consensus/ calls the first four; the decision paths node_up, last_hop
and central_commands). sim is the Committee when the consensus runs on its own,
otherwise the decision path: lasthop.LastHop in C and D, central.CentralPath in
A and B; both have cfg, kernel, net, log, subs and inject().
  begin_run(sim, rng)                        before a run; schedule timed actions
                                             (partitions via sim.net, replays, crashes)
  node_up(sim, node, t) -> bool              crash or offline: a down validator sends
                                             and receives nothing                    exp 2
  proposal(sim, node, height, rnd, block, recipients) -> {recipient: block}
                                             different blocks to different
                                             recipients = equivocation; a recipient
                                             left out gets nothing                   exp 1
  vote(sim, node, kind, height, rnd, value, recipients) -> {recipient: value}
                                             kind "prevote" or "precommit", value a
                                             block id or None (nil): double votes or
                                             withheld votes                          exp 1
  last_hop(sim, src, dst, msg, t) -> bool    a validator's message to a substation is
                                             about to be sent: record it (replay
                                             later with sim.inject) or drop it
                                             (False)                                 exp 4
  central_commands(sim, centre, slot, commands) -> commands
                                             what dispatch centre `centre` sends for
                                             `slot`: a hacked centre (A, B)          exp 5
  compromised(sim) -> set of nodes           whose keys the attacker holds           exp 6

The six faults (design doc table): 1 an equivocating validator; 2 one to three
validators offline; 3 partition or delay; 4 replay of an executed command;
5 restore commands that are each valid but overload the island together;
6 common-cause compromise (several nodes of one organisation at once).
Written so far: Honest, Offline (sites lost: validators, dispatch centres or
substations; experiment 2 will build on it) and Replay (4).
tests/test_consensus.py has first versions of 1 and 2 (Equivocate, and Down,
which Offline generalises).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
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

    def last_hop(self, sim, src: str, dst: str, msg: dict, t: float) -> bool:
        return True

    def central_commands(self, sim, centre: str, slot: int, commands: List) -> List:
        return commands

    def compromised(self, sim) -> Set[str]:
        return set()


class Honest(Fault):
    def __init__(self):
        super().__init__(0.0)


class Offline(Fault):
    """Nodes that send and receive nothing during [start, end): a destroyed or cut-off site.

    One object serves every architecture, each node name counting where it
    exists: validators v0..v6 (C, D; experiment 2), the dispatch centres
    "centre" and "backup" (A, B) or substations. A down node's timers keep
    running, so it resumes when the window ends."""
    label = "offline"

    def __init__(self, nodes: Iterable[str], start: float = 0.0, end: float = math.inf, p_run: float = 1.0):
        super().__init__(p_run)
        self.nodes = frozenset(nodes)
        self.start, self.end = start, end

    def describe(self) -> dict:
        return {**super().describe(), "nodes": sorted(self.nodes), "start": self.start,
                "end": None if math.isinf(self.end) else self.end}

    def node_up(self, sim, node: str, t: float) -> bool:
        return not (self.active and node in self.nodes and self.start <= t < self.end)


def _commands_for(block, substation: str) -> List[dict]:
    payload = block.get("payload") if isinstance(block, dict) else None
    cmds = payload.get("commands") if isinstance(payload, dict) else None
    return [c for c in cmds if isinstance(c, dict) and c.get("substation") == substation] if isinstance(cmds, list) else []


class Replay(Fault):
    """Experiment 4: someone on the last hop resends blocks a substation already executed.

    The attacker sees every delivery to the substations (a hacked dispatch
    centre or relay, or a tapped link: design doc question 25) and resends
    whole deliveries, block and commit proof unchanged (it cannot forge them),
    through sim.inject:

      duplicate  each delivery that carries commands for the substation, again
                 delay_s (default 5 s) after its first copy was sent
      reverse    when a delivery switches a feeder, the latest earlier delivery
                 that set the same feeder the other way, delay_s (default 10 s)
                 later: question 25's "cut feeder 3" sent again after power
                 came back
      late       as reverse, but only deliveries whose dispatch slot started at
                 least min_age_s (default 30 minutes) before, so the TTL stops
                 them if the substation's clock is right
    """
    label = "replay"
    MODES = ("duplicate", "reverse", "late")

    def __init__(self, mode: str = "reverse", delay_s: float = None, min_age_s: float = 1800.0,
                 p_run: float = 1.0):
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}, got {mode!r}")
        super().__init__(p_run)
        self.mode = mode
        self.delay_s = (5.0 if mode == "duplicate" else 10.0) if delay_s is None else delay_s
        self.min_age_s = min_age_s
        self.seen: set = set()
        self.history: Dict[tuple, list] = defaultdict(list)
        self.sent = 0

    def describe(self) -> dict:
        return {**super().describe(), "mode": self.mode, "delay_s": self.delay_s, "min_age_s": self.min_age_s}

    def begin_run(self, sim, rng: random.Random) -> None:
        super().begin_run(sim, rng)
        self.seen, self.history, self.sent = set(), defaultdict(list), 0

    def last_hop(self, sim, src: str, dst: str, msg: dict, t: float) -> bool:
        if not self.active or msg.get("type") != "deliver" or not isinstance(msg.get("block"), dict):
            return True
        block = msg["block"]
        key = (block.get("height"), dst)
        cmds = _commands_for(block, dst)
        if key in self.seen or not cmds:                 # one trigger per block and substation
            return True
        self.seen.add(key)
        when = t + self.delay_s
        if self.mode == "duplicate":
            self._send(sim, dst, msg, when)
        else:
            picks: Dict[int, dict] = {}
            for c in cmds:
                for slot_t, target, old in reversed(self.history[(dst, c.get("feeder"))]):
                    if target != c.get("target") and (self.mode == "reverse" or when - slot_t >= self.min_age_s):
                        picks.setdefault(id(old), old)
                        break
            for old in picks.values():
                self._send(sim, dst, old, when)
        slot_t = block.get("slot", 0) * sim.cfg.dispatch_period_s
        for c in cmds:
            self.history[(dst, c.get("feeder"))].append((slot_t, c.get("target"), msg))
        return True

    def _send(self, sim, dst: str, msg: dict, when: float) -> None:
        self.sent += 1
        block = msg["block"]
        sim.inject(dst, dict(msg, delivery=f"replay:{self.sent}:{dst}", sender="attacker"), when,
                   {"action": "replay", "mode": self.mode, "height": block.get("height"),
                    "of": msg.get("delivery"),
                    "age_s": round(when - block.get("slot", 0) * sim.cfg.dispatch_period_s, 3),
                    "commands": [[c.get("feeder"), c.get("target"), c.get("seq")] for c in _commands_for(block, dst)]})


def build(name: str, **kw) -> Fault:
    """Fault by name; the six experiment faults are added here as they are written."""
    if name.upper() in ("NONE", "HONEST"):
        return Honest()
    if name.lower() == "replay":
        return Replay(**kw)
    if name.lower() == "offline":
        return Offline(**kw)
    raise ValueError(f"unknown or not yet implemented fault {name!r}")
