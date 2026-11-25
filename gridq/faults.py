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
Written: Honest, Offline (sites lost: validators, dispatch centres or
substations; 2), Equivocate (1), Replay (4), HackedCentre (A, B) and
CompromisedValidators (C, D) for 5 and 6, and Observe (6: an honest run that
records what every node receives). Partitions and delay (3) are network
settings (network.py), not faults.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Dict, Iterable, List, Sequence, Set

from .chain import GENESIS
from .events import EventKind


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


class Equivocate(Fault):
    """Experiment 1: compromised validators double-sign.

    As the proposer of a round, a compromised validator sends one block to one
    group of validators and a second block to the other; in such a round every
    compromised validator votes for the first block to the first group and for
    the second block to the second, prevotes and precommits alike. The groups
    split the honest validators in half at random, drawn per run, height and
    round, with every compromised validator in the first: the attacker's best
    split. Within f the honest validators commit at most one of the two blocks;
    with f + 1 compromised both reach Q and two different blocks are committed
    at one height.

    The second block must pass the honest validators' own checks. In
    run_committee there is no application check, so it is the same header with
    another payload. In C and D's island (a lasthop.ChainDispatch) it is the
    block without the fresh state reports, its commands recomputed for the older
    view: equally valid, since a proposer may leave reports out. With no fresh
    report there is no second valid block, and the proposer behaves honestly in
    that round. When not proposing, a compromised validator runs the protocol
    honestly, except for its votes in rounds that another compromised validator
    split.
    """
    label = "equivocate"

    def __init__(self, bad: Iterable[str], groups=None, p_run: float = 1.0):
        super().__init__(p_run)
        self.bad = frozenset(bad)
        self.groups = None if groups is None else tuple(frozenset(g) for g in groups)
        self.pairs: Dict[tuple, tuple] = {}         # (height, round) -> (first block id, second block id)
        self.token = 0

    def describe(self) -> dict:
        return {**super().describe(), "bad": sorted(self.bad),
                "groups": None if self.groups is None else [sorted(g) for g in self.groups]}

    def begin_run(self, sim, rng: random.Random) -> None:
        super().begin_run(sim, rng)
        self.pairs, self.token = {}, rng.getrandbits(32)

    def compromised(self, sim) -> Set[str]:
        return set(self.bad) if self.active else set()

    def split(self, height: int, rnd: int, recipients: Iterable[str]):
        """(first group, second group) for this height and round."""
        if self.groups is not None:
            return self.groups
        honest = sorted(r for r in recipients if r not in self.bad)
        random.Random(f"split-{self.token}-{height}-{rnd}").shuffle(honest)
        k = len(honest) // 2
        return frozenset(honest[:k]) | self.bad, frozenset(honest[k:])

    def second_block(self, sim, node: str, block):
        """Another block the honest validators accept at this height and round, or None."""
        dispatch = getattr(sim, "dispatch", None)
        if dispatch is None:                                   # no application check
            return dict(block, payload={"second": block.get("round")})
        payload = block.get("payload") if isinstance(block, dict) else None
        if not isinstance(payload, dict) or not payload.get("reports"):
            return None
        view, counts = dispatch.chain_state(sim.com.nodes[node])
        extras = {k: payload[k] for k in dispatch.EXTRA}
        return dict(block, payload={"reports": [], **extras,
                                    "commands": dispatch.block_commands(view, counts, block["slot"], extras)})

    def proposal(self, sim, node: str, height: int, rnd: int, block, recipients: Iterable[str]) -> Dict[str, object]:
        recipients = list(recipients)
        if not self.active or node not in self.bad:
            return super().proposal(sim, node, height, rnd, block, recipients)
        other = self.second_block(sim, node, block)
        if other is None:
            return super().proposal(sim, node, height, rnd, block, recipients)
        from .consensus.messages import block_id               # here: consensus imports this module
        g1, g2 = self.split(height, rnd, recipients)
        self.pairs[(height, rnd)] = (block_id(block), block_id(other))
        return {r: (block if r in g1 else other) for r in recipients if r in g1 | g2}

    def vote(self, sim, node: str, kind: str, height: int, rnd: int, value,
             recipients: Iterable[str]) -> Dict[str, object]:
        recipients = list(recipients)
        pair = self.pairs.get((height, rnd))
        if not self.active or node not in self.bad or pair is None:
            return super().vote(sim, node, kind, height, rnd, value, recipients)
        g1, g2 = self.split(height, rnd, recipients)
        return {r: (pair[0] if r in g1 else pair[1]) for r in recipients if r in g1 | g2}


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


# ---------------------------------------------------------------- experiment 5: hacked decision makers

ATTACK_MODES = ("restore_all", "cut_critical", "starve", "freeze")


def attack_targets(mode: str, cfg, specs, view, measurement, slot: int, starve: Sequence[str] = ("S4",)) -> Dict[str, str]:
    """The feeder targets an attacker who controls a decision sends (experiments 5 and 6).

      restore_all   every feeder on: each command alone is one a dispatcher may
                    send, together they draw far more than the island has
      cut_critical  the policy's targets, but every critical feeder off: the
                    obvious violation B's local rule refuses
      starve        the policy's targets, but the non-critical feeders of the
                    substations in `starve` off all the time: each command is
                    legal, the rotation is not fair
      freeze        every feeder as it is: no command at all, while the decision
                    still looks alive (experiment 6: a hacked centre stalls the
                    rotation alone)"""
    from .policy import decide                     # here: policy imports verifier, which imports consensus
    if mode not in ATTACK_MODES:
        raise ValueError(f"mode must be one of {ATTACK_MODES}, got {mode!r}")
    if mode == "restore_all":
        return {sp.name: "on" for sp in specs}
    if mode == "freeze":
        return {sp.name: view[sp.substation]["states"][sp.name] for sp in specs}
    targets = dict(decide(cfg, specs, view, measurement, slot))
    for sp in specs:
        if (mode == "cut_critical" and sp.critical) or (mode == "starve" and not sp.critical
                                                        and sp.substation in starve):
            targets[sp.name] = "off"
    return targets


class HackedCentre(Fault):
    """Experiments 5 and 6 in A and B: an attacker controls what a dispatch centre sends during [start, end).

    The centre still signs with its own key, so its commands are authentic: they
    carry attack_targets(mode) for the centre's own view, numbered and versioned
    as the centre would number them. A's substations execute whatever verifies;
    B's local enforcer refuses cutting a critical feeder and more than
    cfg.local_max_switch switchings per period, but it sees one substation and
    one command at a time. The centre keeps its heartbeat and its alive tokens,
    so with "freeze" (no commands) neither the backup nor B's table takes over:
    one node stalls the rotation."""
    label = "hacked_centre"

    def __init__(self, mode: str, centres: Iterable[str] = ("centre",), start: float = 0.0, end: float = math.inf,
                 p_run: float = 1.0):
        if mode not in ATTACK_MODES:
            raise ValueError(f"mode must be one of {ATTACK_MODES}, got {mode!r}")
        super().__init__(p_run)
        self.mode, self.centres = mode, frozenset(centres)
        self.start, self.end = start, end
        self.sent = 0

    def describe(self) -> dict:
        return {**super().describe(), "mode": self.mode, "centres": sorted(self.centres), "start": self.start,
                "end": None if math.isinf(self.end) else self.end}

    def begin_run(self, sim, rng: random.Random) -> None:
        super().begin_run(sim, rng)
        self.sent = 0

    def compromised(self, sim) -> Set[str]:
        return set(self.centres) if self.active else set()

    def central_commands(self, sim, centre: str, slot: int, commands: List) -> List:
        t = sim.kernel.now
        if not self.active or centre not in self.centres or not self.start <= t < self.end:
            return commands
        from .policy import commands_for
        c = sim.centres[centre]
        view = sim.view(c)
        targets = attack_targets(self.mode, sim.cfg, sim.specs, view, sim.measure(slot), slot)
        out = commands_for(sim.cfg, targets, view, {s: max(c.issued[s], view[s]["last_seq"]) for s in view}, slot)
        self.sent += len(out)
        sim.log.append(t, EventKind.FAULT_ACTION, c.actor, {"action": "hacked_commands", "mode": self.mode,
                                                           "slot": slot, "honest": len(commands), "sent": len(out)},
                       "truth")
        return out


class CompromisedValidators(Fault):
    """Experiments 5 and 6 in C and D: an attacker holds the keys of the validators in `bad`.

    During [start, end) it does two things:
      propose  as the proposer of a round, a compromised validator proposes the
               agreed block (same reports and measurements) with
               attack_targets(mode) as its commands. Every honest validator
               recomputes the commands (lasthop.PolicyDispatch) and prevotes
               nil, so the round fails and an honest proposer's block commits
               a few seconds later.
      forge    at every dispatch slot start it signs, with every key it holds,
               a commit proof for its own next block on the chain the
               substations follow (attack commands for the newest state reports
               the compromised validators hold), and sends it to every
               substation (sim.inject) before the committee can commit. With
               fewer than Q keys the proof is short and every substation refuses
               it (insufficient_quorum). With Q or more it is a valid commit:
               the substations apply it and from then on refuse the committee's
               blocks (stale_seq, header_mismatch), so the attacker's chain is
               the one they follow.
    Otherwise the compromised validators run the protocol honestly (they could
    also stall it from f + 1 on by not voting; experiments 1 and 2 cover that).
    """
    label = "compromised_validators"

    def __init__(self, bad: Iterable[str], mode: str, start: float = 0.0, end: float = math.inf,
                 forge: bool = True, p_run: float = 1.0):
        if mode not in ATTACK_MODES:
            raise ValueError(f"mode must be one of {ATTACK_MODES}, got {mode!r}")
        super().__init__(p_run)
        self.bad, self.mode, self.forge = tuple(sorted(bad)), mode, forge
        self.start, self.end = start, end
        self.sim = None
        self.tip = None                     # (height, hash) of the newest block the substations follow
        self.proposed: Set[str] = set()     # ids of the attack blocks proposed
        self.forged: List[tuple] = []       # (height, block id, commands) sent to the substations

    def describe(self) -> dict:
        return {**super().describe(), "bad": list(self.bad), "mode": self.mode, "forge": self.forge,
                "start": self.start, "end": None if math.isinf(self.end) else self.end}

    def begin_run(self, sim, rng: random.Random) -> None:
        super().begin_run(sim, rng)
        self.sim, self.tip, self.proposed, self.forged = sim, None, set(), []
        if not (self.active and self.forge and self.bad and hasattr(sim, "inject") and hasattr(sim, "dispatch")):
            return
        period = sim.cfg.dispatch_period_s
        k = max(0, math.ceil(self.start / period - 1e-9))
        while k < sim.cfg.n_dispatches and k * period < self.end:
            sim.timer(k * period, self._forge, k)
            k += 1

    def compromised(self, sim) -> Set[str]:
        return set(self.bad) if self.active else set()

    def _window(self, t: float) -> bool:
        return self.active and self.start <= t < self.end

    def _attack_commands(self, view, counts, measurement, slot: int) -> List[dict]:
        from .policy import commands_for
        d = self.sim.dispatch
        return commands_for(self.sim.cfg, attack_targets(self.mode, self.sim.cfg, d.specs, view, measurement, slot),
                            view, counts, slot)

    def proposal(self, sim, node: str, height: int, rnd: int, block, recipients: Iterable[str]) -> Dict[str, object]:
        recipients = list(recipients)
        dispatch = getattr(sim, "dispatch", None)
        payload = block.get("payload") if isinstance(block, dict) else None
        if (node not in self.bad or dispatch is None or not isinstance(payload, dict)
                or not self._window(sim.kernel.now)):
            return super().proposal(sim, node, height, rnd, block, recipients)
        from .consensus.messages import block_id
        view, counts = dispatch.chain_state(sim.com.nodes[node])
        view = dispatch._updated(view, payload.get("reports", ()))
        cmds = self._attack_commands(view, counts, payload["measurements"], block["slot"])
        if cmds == payload.get("commands"):                    # the attack changes nothing in this slot
            return super().proposal(sim, node, height, rnd, block, recipients)
        bad_block = dict(block, payload=dict(payload, commands=cmds))
        self.proposed.add(block_id(bad_block))
        return {r: bad_block for r in recipients}

    def last_hop(self, sim, src: str, dst: str, msg: dict, t: float) -> bool:
        """Follow the committee's chain until the attacker's own blocks take over."""
        block, proof = msg.get("block"), msg.get("proof")
        if (msg.get("type") == "deliver" and not self._captured() and isinstance(block, dict)
                and isinstance(proof, dict) and (self.tip is None or block.get("height", 0) > self.tip[0])):
            self.tip = (block["height"], proof["block"])
        return True

    def _captured(self) -> bool:
        return bool(self.forged) and len(self.bad) >= self.sim.cfg.quorum

    def _forge(self, k: int, t: float) -> None:
        from .consensus.messages import PRECOMMIT, block_id, make_block, make_proof, vote_digest
        sim, cfg = self.sim, self.sim.cfg
        d = sim.dispatch
        h, prev = self.tip if self.tip is not None else (0, GENESIS)
        start = d.start_state()
        mine = sim.reports.get(self.bad[0], {})
        view = {s: (d.entry(mine[s]) if s in mine else start[s]) for s in start}
        counts = {s: view[s]["last_seq"] for s in view}
        meas = d.measure(k)
        cmds = self._attack_commands(view, counts, meas, k)
        block = make_block(cfg, h + 1, prev, k, self.bad[0], 0, {"reports": [], "measurements": meas, "commands": cmds})
        bid = block_id(block)
        digest = vote_digest(cfg, PRECOMMIT, h + 1, 0, bid)
        proof = make_proof(h + 1, 0, bid, {v: sim.com.keys.sign(v, digest) for v in self.bad})
        self.forged.append((h + 1, bid, len(cmds)))
        for s in sorted(sim.subs):
            sim.inject(s, {"type": "deliver", "block": block, "proof": proof, "sender": "attacker",
                           "delivery": f"forge:{h + 1}:{s}"}, t,
                       {"action": "forge", "height": h + 1, "keys": len(self.bad), "commands": len(cmds)})
        if len(self.bad) >= cfg.quorum:
            self.tip = (h + 1, bid)


# ---------------------------------------------------------------- experiment 6: what each node sees

class Observe(Fault):
    """Experiment 6 (design doc question 29): an honest run that records what each node receives.

    Logs a truth event (FAULT_ACTION "observed") the first time a node gets a
    substation's feeder states ("states": a state report), a substation's
    commands ("commands") or the island's measurements ("measurements": the
    supply and every feeder's demand). Validators (C, D) get every proposal;
    substations every message on their last hop (whole blocks in C and D,
    their own batch in A and B) besides knowing their own state; dispatch
    centres (A, B) hold every state report, standby or active, and the active
    one makes every command from the slot's measurements. Behaviour is
    unchanged. metrics.data_exposure reads the events back."""
    label = "observe"

    def __init__(self):
        super().__init__(1.0)
        self.seen: Set[tuple] = set()

    def begin_run(self, sim, rng: random.Random) -> None:
        super().begin_run(sim, rng)
        self.seen = set()
        for s in sorted(getattr(sim, "subs", None) or ()):
            self._saw(sim, s, "states", s)

    def _saw(self, sim, node: str, what: str, sub=None) -> None:
        if (node, what, sub) in self.seen or getattr(sim, "log", None) is None:
            return
        self.seen.add((node, what, sub))
        sim.log.append(sim.kernel.now, EventKind.FAULT_ACTION, "observer",
                       {"action": "observed", "node": node, "what": what, "substation": sub}, "truth")

    def _payload(self, sim, node: str, block) -> None:
        payload = block.get("payload") if isinstance(block, dict) else None
        if not isinstance(payload, dict):
            return
        for r in payload.get("reports") or ():
            if isinstance(r, dict):
                self._saw(sim, node, "states", r.get("substation"))
        for c in payload.get("commands") or ():
            if isinstance(c, dict):
                self._saw(sim, node, "commands", c.get("substation"))
        if payload.get("measurements"):
            self._saw(sim, node, "measurements")

    def proposal(self, sim, node: str, height: int, rnd: int, block, recipients: Iterable[str]) -> Dict[str, object]:
        out = super().proposal(sim, node, height, rnd, block, recipients)
        self._payload(sim, node, block)
        for r, b in out.items():
            self._payload(sim, r, b)
        return out

    def last_hop(self, sim, src: str, dst: str, msg: dict, t: float) -> bool:
        kind = msg.get("type")
        if kind == "deliver":
            self._payload(sim, dst, msg.get("block"))
        elif kind == "sync":
            for entry in msg.get("entries") or ():
                if isinstance(entry, (list, tuple)) and entry:
                    self._payload(sim, dst, entry[0])
        elif kind == "batch":
            for env in msg.get("commands") or ():
                cmd = env.get("command") if isinstance(env, dict) else None
                if isinstance(cmd, dict):
                    self._saw(sim, dst, "commands", cmd.get("substation"))
        return True

    def central_commands(self, sim, centre: str, slot: int, commands: List) -> List:
        for name, c in sorted(sim.centres.items()):
            for s in sorted(c.reports):
                self._saw(sim, name, "states", s)
        for cmd in commands:
            self._saw(sim, centre, "commands", cmd.get("substation"))
        self._saw(sim, centre, "measurements")
        return commands


def build(name: str, **kw) -> Fault:
    """Fault by name; the six experiment faults are added here as they are written."""
    if name.upper() in ("NONE", "HONEST"):
        return Honest()
    if name.lower() == "replay":
        return Replay(**kw)
    if name.lower() == "offline":
        return Offline(**kw)
    if name.lower() == "equivocate":
        return Equivocate(**kw)
    if name.lower() == "hacked_centre":
        return HackedCentre(**kw)
    if name.lower() == "compromised_validators":
        return CompromisedValidators(**kw)
    if name.lower() == "observe":
        return Observe()
    raise ValueError(f"unknown or not yet implemented fault {name!r}")
