"""Tendermint-style BFT consensus with one state machine per validator (new in gridq).

Follows Algorithm 1 of Buchman, Kwon and Milosevic, "The latest gossip on BFT
consensus" (2018), the protocol CometBFT runs: propose, prevote, precommit,
locks, valid values, round changes, f + 1 round skipping. "2f + 1" is the quorum
Q = cfg.quorum and "f + 1" is more than n/3 (n // 3 + 1). Modelling choices,
needed because this network drops messages instead of gossiping them until
they arrive (network.py):

  timers     every step ends by a timer, whatever arrives: propose after
             cfg.round_timeout(r) (call it P), prevote vote_timeout(r) (V) after
             max(entering prevote, P), precommit V after max(entering
             precommit, P + V), all counted from the node's start of round r.
             A round that fails therefore ends P + 2V after it started for
             every node, which is the paper's timing when all votes arrive:
             there the prevote and precommit timers start after 2f + 1 votes
             and gossip delivers the rest, but here a lost vote could then
             stall a round for ever. Timers decide when a node moves on, never
             what it votes, so safety is unaffected.
  POL        a proposal that re-proposes the proposer's valid value carries the
             Q prevote signatures that made it valid (its proof of lock). In
             CometBFT those prevotes are gossiped. Without them a node that
             missed them could never accept the value, while locked nodes never
             accept another one, so one unlucky round could stall a height.
  votes      each signed (voter, value) counts once toward its value, so a
             proof of lock or any Q signatures count in full at every node,
             even one that a double signer sent another vote first (CometBFT
             keeps such conflicting votes once a peer claims a majority for
             them). Within f no two values reach Q in one round.
  bodies     a node prevotes or precommits a block only if it got the proposal
             (CometBFT precommits nil on a polka for a block it lacks), but a
             precommit carries its block, standing in for CometBFT's block-part
             gossip, so any node that counts Q precommits can commit.
  catch-up   a node that hears a message for a height it already decided
             answers with its blocks and commit proofs from that height on,
             once per peer, height and round (CometBFT's block sync). The
             receiver counts the proof's precommits as votes it heard, so a
             validator that precommitted another block to it in that round is
             caught double-signing (EVIDENCE), as CometBFT's vote gossip
             would catch it.
  slots      block h records the dispatch slot k it was proposed in; height
             h + 1 starts when slot k + 1 starts, or at once if consensus ran
             past it. A block is valid if chain id, epoch, height, prev hash and
             slot fit (previous slot < slot <= current slot) and the
             application check valid_fn accepts it. valid_fn must give the same
             answer for the same block at any time; a stale block is the
             substation verifier's business. (A locked block that later turned
             invalid would stall the height.)

Faults come only through the faults.py hooks, called with sim = the committee
when it runs on its own (run_committee): node_up (a down node sends and
receives nothing, its timers keep running), proposal and vote (what a
compromised node sends to whom; it can sign only with its own key).

Logged (public): PROPOSAL, PREVOTE and PRECOMMIT once per distinct value sent,
COMMIT per node with its commit proof, ROUND_TIMEOUT, EVIDENCE. Truth only:
FAULT_ACTION when a hook made a node deviate.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..chain import GENESIS
from ..crypto import KeyRing
from ..events import EventKind, EventLog
from ..faults import Fault, Honest
from ..kernel import PRIO_MESSAGE, Kernel
from ..network import Network
from .messages import (PRECOMMIT, PREVOTE, block_id, make_block, make_proof, proposal_digest,
                       verify_commit, vote_digest)

PROPOSE = "propose"
FUTURE_CAP = 64           # early messages for the next height a node keeps


class Validator:
    """One validator's view: decided chain, current height, round, step, lock."""

    def __init__(self, name: str, com: "Committee"):
        self.name = name
        self.com = com
        self.cfg = com.cfg
        self.actor = f"validator:{name}"
        self.chain: List[dict] = []      # decided blocks as chain.py headers (block + "hash")
        self.proofs: List[dict] = []     # the commit proof of each decided block
        self.height = 1
        self.future: List[dict] = []     # messages for height + 1 that arrived early
        self.synced: set = set()         # (peer, height, round) catch-up answers already sent
        self._reset()

    def _reset(self) -> None:
        """Fresh state for a new height."""
        self.round = -1                  # -1 until the height starts
        self.round_start = 0.0           # when this node started its current round
        self.step: Optional[str] = None
        self.locked: Optional[Tuple[dict, str]] = None    # (block, id)
        self.locked_round = -1
        self.valid: Optional[Tuple[dict, str]] = None
        self.valid_round = -1
        self.proposals: Dict[int, Tuple[object, str, int]] = {}   # round -> (block, id, valid_round)
        self.bodies: Dict[str, dict] = {}                        # id -> every block seen at this height
        self.prevotes: Dict[int, Dict[str, dict]] = defaultdict(dict)     # round -> voter -> {id | None: sig}
        self.precommits: Dict[int, Dict[str, dict]] = defaultdict(dict)
        self.tally: Dict[tuple, Counter] = defaultdict(Counter)  # (kind, round) -> id | None -> votes
        self.ready: List[Tuple[int, str]] = []                   # (round, id) with Q precommits
        self.heard: Dict[int, set] = defaultdict(set)            # round -> validators heard in it
        self.skip_round = -1                                     # latest round heard from > n/3
        self.polka_done: set = set()                             # rounds whose polka rule fired
        self.reported: set = set()                               # evidence already logged

    @property
    def last_hash(self) -> str:
        return self.chain[-1]["hash"] if self.chain else GENESIS

    @property
    def last_slot(self) -> int:
        return self.chain[-1]["slot"] if self.chain else -1

    def is_valid(self, block, prevote: bool = False) -> bool:
        """Header fits this node's chain and clock, and the application check accepts it.
        prevote: also the node's own check before it prevotes for a new proposal (prevote_fn),
        which may depend on what this node has seen; a re-proposal with its proof of lock,
        locking and committing do not use it, so it can cost a round but never stall a height."""
        if not isinstance(block, dict):
            return False
        c, slot = self.cfg, block.get("slot")
        if (block.get("chain_id") != c.chain_id or block.get("epoch") != c.epoch
                or block.get("height") != self.height or block.get("prev") != self.last_hash
                or not isinstance(slot, int) or not self.last_slot < slot <= self.com.slot_of(self.com.kernel.now)):
            return False
        if self.com.valid_fn is not None and not self.com.valid_fn(self, block):
            return False
        return not prevote or self.com.prevote_fn is None or bool(self.com.prevote_fn(self, block))

    # ---------- rounds and steps ----------
    def start_height(self, height: int, t: float) -> None:
        if height == self.height and self.round < 0:
            self.start_round(0, t)

    def start_round(self, r: int, t: float) -> None:
        self.round, self.step, self.round_start = r, PROPOSE, t
        self.com.round_started(self, t)
        if self.com.proposer(self.height, r) == self.name:
            if self.valid is not None:
                (block, bid), vr = self.valid, self.valid_round
                pol = {v: sigs[bid] for v, sigs in self.prevotes[vr].items() if bid in sigs}
            else:
                block, vr, pol = self._new_block(r, t), -1, None
            self.com.send_proposal(self, block, vr, pol, t)
        self.com.timer(t + self.cfg.round_timeout(r), self._timeout, PROPOSE, self.height, r)
        self._evaluate(t)

    def _new_block(self, r: int, t: float) -> dict:
        payload = self.com.propose_fn(self, self.height, r, t) if self.com.propose_fn else {}
        return make_block(self.cfg, self.height, self.last_hash, self.com.slot_of(t), self.name, r, payload)

    def _vote(self, kind: str, bid: Optional[str], t: float) -> None:
        self.step = kind
        self.com.send_vote(self, kind, bid, t)
        r = self.round
        v = self.cfg.vote_timeout(r)
        not_before = self.round_start + self.cfg.round_timeout(r) + (v if kind == PRECOMMIT else 0.0)
        self.com.timer(max(t, not_before) + v, self._timeout, kind, self.height, r)

    def _timeout(self, step: str, height: int, r: int, t: float) -> None:
        if (self.height, self.round, self.step) != (height, r, step):
            return
        self.com.log_timeout(self, t)
        if step == PROPOSE:
            self._vote(PREVOTE, None, t)
            self._evaluate(t)
        elif step == PREVOTE:
            self._vote(PRECOMMIT, None, t)
            self._evaluate(t)
        else:
            self.start_round(r + 1, t)

    # ---------- receiving ----------
    def on_message(self, msg: dict, t: float) -> None:
        if msg.get("type") == "commit":
            self._on_catch_up(msg, t)
            return
        h = msg.get("height")
        if not isinstance(h, int):
            return
        if h > self.height:
            if h == self.height + 1 and len(self.future) < FUTURE_CAP:
                self.future.append(msg)
            return
        if not self.com.authentic(msg):
            self.com.stats["rejected"] += 1
            return
        if h < self.height:
            proof = self.proofs[h - 1]
            if not (msg["type"] != "proposal" and msg["round"] == proof["round"]
                    and msg.get("block_id") == proof["block"]):   # a vote that helped commit: sender is on track
                self.com.send_catch_up(self, msg["sender"], h, msg["round"], t)
            return
        if msg["type"] == "proposal":
            self._on_proposal(msg)
        else:
            self._on_vote(msg)
        r, who = msg["round"], self.heard[msg["round"]]
        who.add(msg["sender"])
        if r > self.skip_round and len(who) >= self.com.weak:
            self.skip_round = r
        self._evaluate(t)

    def _on_proposal(self, msg: dict) -> None:
        r, block, vr = msg["round"], msg["block"], msg["valid_round"]
        bid = block_id(block)
        old = self.proposals.get(r)
        if old is not None:
            if old[1] != bid:
                self._evidence(msg["sender"], "proposal", r, old[1], bid)
            return
        self.proposals[r] = (block, bid, vr)
        if isinstance(block, dict):
            self.bodies.setdefault(bid, block)
        pol = msg.get("pol")
        if 0 <= vr < r and isinstance(pol, dict):
            digest = vote_digest(self.cfg, PREVOTE, self.height, vr, bid)
            for voter, sig in pol.items():
                if self.com.keys.verify(voter, digest, sig):
                    self.add_vote(PREVOTE, vr, voter, bid, sig)

    def _on_vote(self, msg: dict) -> None:
        kind, bid, body = msg["type"], msg.get("block_id"), msg.get("block")
        if (kind == PRECOMMIT and bid is not None and bid not in self.bodies
                and isinstance(body, dict) and block_id(body) == bid):
            self.bodies[bid] = body
        self.add_vote(kind, msg["round"], msg["sender"], bid, msg["sig"])

    def add_vote(self, kind: str, r: int, voter: str, bid: Optional[str], sig: str) -> None:
        """Record a verified vote. Each signed (voter, value) counts once toward that value,
        so Q signatures for one block always count as Q, even if some signer sent this node
        another vote in that round (as CometBFT's vote set tracks conflicting votes once a
        peer claims a majority for them): otherwise a node that heard a double signer's
        other vote first could never accept a proof of lock, and a height could stall with
        honest nodes locked on different blocks. Within f this is safe: two blocks with Q
        signatures each in one round need f + 1 double signers. A second, different vote
        of one voter in one round is also evidence of double signing."""
        votes = (self.prevotes if kind == PREVOTE else self.precommits)[r].setdefault(voter, {})
        if bid in votes:
            return
        if votes:
            self._evidence(voter, kind, r, next(iter(votes)), bid)
        votes[bid] = sig
        c = self.tally[(kind, r)]
        c[bid] += 1
        if kind == PRECOMMIT and bid is not None and c[bid] == self.com.q:
            self.ready.append((r, bid))

    def _evidence(self, offender: str, kind: str, r: int, a, b) -> None:
        if (offender, kind, r) not in self.reported:
            self.reported.add((offender, kind, r))
            self.com.log_evidence(self, offender, kind, r, a, b)

    def _on_catch_up(self, msg: dict, t: float) -> None:
        for item in msg.get("entries") or ():
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                return
            block, proof = item
            h = block.get("height") if isinstance(block, dict) else None
            if not isinstance(h, int) or h > self.height:
                return
            if h < self.height:
                continue
            if verify_commit(self.com.keys, self.cfg, block, proof) or not self.is_valid(block):
                return
            digest = vote_digest(self.cfg, PRECOMMIT, h, proof["round"], proof["block"])
            for voter, sig in sorted(proof["sigs"].items()):   # the proof's precommits count as heard,
                if voter in self.com.nodes and self.com.keys.verify(voter, digest, sig):   # so a double vote shows
                    self.add_vote(PRECOMMIT, proof["round"], voter, proof["block"], sig)
            self._decide(block, proof, "sync", t)

    # ---------- the upon-rules ----------
    def _evaluate(self, t: float) -> None:
        while self._apply_rule(t):
            pass

    def _count(self, kind: str, r: int, bid: Optional[str]) -> int:
        c = self.tally.get((kind, r))
        return c[bid] if c else 0

    def _apply_rule(self, t: float) -> bool:
        """Apply the first rule whose condition holds; True if the node should look again."""
        q = self.com.q
        if self._try_commit(t):
            return False                               # new height; it starts by its own timer
        if self.skip_round > self.round:               # more than n/3 are already in a later round
            self.start_round(self.skip_round, t)
            return False
        R = self.round
        if R < 0:
            return False
        prop = self.proposals.get(R)
        if self.step == PROPOSE and prop is not None:
            block, bid, vr = prop
            locked_bid = self.locked[1] if self.locked else None
            if vr == -1:
                ok = self.is_valid(block, prevote=True) and (self.locked_round == -1 or locked_bid == bid)
                self._vote(PREVOTE, bid if ok else None, t)
                return True
            if 0 <= vr < R and self._count(PREVOTE, vr, bid) >= q:
                ok = self.is_valid(block) and (self.locked_round <= vr or locked_bid == bid)   # Q already prevoted it
                self._vote(PREVOTE, bid if ok else None, t)
                return True
        if prop is not None and self.step != PROPOSE and R not in self.polka_done:
            block, bid, _ = prop
            if self._count(PREVOTE, R, bid) >= q and self.is_valid(block):
                self.polka_done.add(R)
                self.valid, self.valid_round = (block, bid), R
                if self.step == PREVOTE:
                    self.locked, self.locked_round = (block, bid), R
                    self._vote(PRECOMMIT, bid, t)
                return True
        if self.step == PREVOTE and self._count(PREVOTE, R, None) >= q:
            self._vote(PRECOMMIT, None, t)
            return True
        return False

    def _try_commit(self, t: float) -> bool:
        """Commit on Q precommits for one block of any round, once the block itself is here."""
        for r, bid in self.ready:
            body = self.bodies.get(bid)
            if body is not None and self.is_valid(body):
                sigs = {v: s[bid] for v, s in self.precommits[r].items() if bid in s}
                self._decide(body, make_proof(self.height, r, bid, sigs), "votes", t)
                return True
        return False

    def _decide(self, block: dict, proof: dict, via: str, t: float) -> None:
        entry = dict(block, hash=proof["block"])
        self.chain.append(entry)
        self.proofs.append(proof)
        self.com.decided(self, entry, proof, via, t)
        self.height += 1
        self._reset()
        self.com.schedule_height(self, t)
        if self.future:
            pending, self.future = self.future, []
            self.com.timer(t, self._replay, pending)

    def _replay(self, pending: List[dict], t: float) -> None:
        for m in pending:
            if m.get("height", 0) >= self.height:
                self.on_message(m, t)


class Committee:
    """n validators, the network between them, and what they decided.

    propose_fn(node, height, round, t) -> payload    the block content (default {})
    valid_fn(node, block) -> bool                    application validity (default: accept); the
                                                     same answer at every node and any time
    prevote_fn(node, block) -> bool                  the node's own extra check before it prevotes
                                                     for a proposal (default: none); may differ
                                                     between nodes, so it never blocks a commit
    on_decide(node, header, proof, t)                called at every node's commit
    """

    def __init__(self, cfg, kernel: Kernel, net: Network, *, keys: Optional[KeyRing] = None,
                 log: Optional[EventLog] = None, fault: Optional[Fault] = None, sim=None,
                 names: Optional[Sequence[str]] = None, propose_fn: Optional[Callable] = None,
                 valid_fn: Optional[Callable] = None, on_decide: Optional[Callable] = None,
                 prevote_fn: Optional[Callable] = None):
        self.cfg, self.kernel, self.net = cfg, kernel, net
        self.names = list(names) if names is not None else [f"v{i}" for i in range(cfg.n_validators)]
        if len(self.names) != cfg.n_validators or len(set(self.names)) != len(self.names):
            raise ValueError("need cfg.n_validators distinct validator names")
        self.keys = keys if keys is not None else KeyRing(self.names, cfg.seed)
        self.log = log if log is not None else EventLog()
        self.fault = fault if fault is not None else Honest()
        self.sim = sim if sim is not None else self
        self.propose_fn, self.valid_fn, self.on_decide = propose_fn, valid_fn, on_decide
        self.prevote_fn = prevote_fn
        self.q = cfg.quorum
        self.weak = cfg.n_validators // 3 + 1          # more than n/3: at least one honest node
        self.n_slots = cfg.n_dispatches                 # no height starts in a later slot
        self.stats: Counter = Counter()
        self.decisions: Dict[int, Dict[str, dict]] = defaultdict(dict)   # height -> node -> commit
        self.starts: Dict[int, Dict[str, float]] = defaultdict(dict)     # height -> node -> round-0 start
        self.nodes: Dict[str, Validator] = {n: Validator(n, self) for n in self.names}

    # ---------- time ----------
    def start(self, t: float = 0.0) -> None:
        for v in self.nodes.values():
            self.schedule_height(v, t)

    def timer(self, t: float, fn: Callable, *args) -> None:
        """Call fn(*args, now) at time t."""
        self.kernel.schedule(t, PRIO_MESSAGE, self._fire, fn, args)

    def _fire(self, fn: Callable, args: tuple) -> None:
        fn(*args, self.kernel.now)

    def slot_of(self, t: float) -> int:
        return int(math.floor(t / self.cfg.dispatch_period_s + 1e-9))

    def proposer(self, height: int, rnd: int) -> str:
        return self.names[(height + rnd) % len(self.names)]

    def schedule_height(self, node: Validator, t: float) -> None:
        """Start the node's next height when the slot after its last block starts."""
        slot = node.last_slot + 1
        if slot < self.n_slots:
            self.timer(max(t, slot * self.cfg.dispatch_period_s), node.start_height, node.height)

    def round_started(self, node: Validator, t: float) -> None:
        if node.round == 0:
            self.starts[node.height].setdefault(node.name, t)

    # ---------- messages ----------
    def up(self, node: str, t: float) -> bool:
        return self.fault.node_up(self.sim, node, t)

    def _send(self, src: str, dst: str, msg: dict, msg_id: str, t: float) -> None:
        self.stats["sent"] += 1
        arrival = self.net.deliver(msg_id, src, dst, t)
        if arrival is None:
            self.stats["lost"] += 1
        else:
            self.kernel.schedule(arrival, PRIO_MESSAGE, self._arrive, dst, msg)

    def _arrive(self, dst: str, msg: dict) -> None:
        t = self.kernel.now
        if not self.up(dst, t):
            self.stats["dropped_down"] += 1
            return
        self.nodes[dst].on_message(msg, t)

    def authentic(self, msg: dict) -> bool:
        """Signed by its sender, a validator; a proposal also by the round's proposer."""
        typ, s, h, r = msg.get("type"), msg.get("sender"), msg.get("height"), msg.get("round")
        if s not in self.nodes or not isinstance(r, int) or r < 0:
            return False
        if typ == "proposal":
            vr = msg.get("valid_round")
            if s != self.proposer(h, r) or not isinstance(vr, int):
                return False
            digest = proposal_digest(self.cfg, h, r, block_id(msg.get("block")), vr)
        elif typ in (PREVOTE, PRECOMMIT):
            digest = vote_digest(self.cfg, typ, h, r, msg.get("block_id"))
        else:
            return False
        return self.keys.verify(s, digest, msg.get("sig"))

    def send_proposal(self, node: Validator, block: dict, vr: int, pol: Optional[dict], t: float) -> None:
        if not self.up(node.name, t):
            return
        h, r = node.height, node.round
        sent = self.fault.proposal(self.sim, node.name, h, r, block, list(self.names))
        ids: Dict[int, str] = {}
        msgs: Dict[str, dict] = {}
        groups: Dict[str, List[str]] = defaultdict(list)
        msg_id = f"proposal:{h}:{r}:{node.name}"
        for dst in self.names:
            if dst not in sent:
                continue
            blk = sent[dst]
            bid = ids.get(id(blk))
            if bid is None:
                bid = ids[id(blk)] = block_id(blk)
            if bid not in msgs:
                msgs[bid] = {"type": "proposal", "height": h, "round": r, "block": blk, "valid_round": vr,
                             "pol": pol, "sender": node.name,
                             "sig": self.keys.sign(node.name, proposal_digest(self.cfg, h, r, bid, vr))}
                if isinstance(blk, dict):
                    node.bodies.setdefault(bid, blk)     # the proposer knows every block it sent
            groups[bid].append(dst)
            self._send(node.name, dst, msgs[bid], msg_id, t)
        self._log_sent(EventKind.PROPOSAL, node, groups, block_id(block), t, valid_round=vr)

    def send_vote(self, node: Validator, kind: str, bid: Optional[str], t: float) -> None:
        if not self.up(node.name, t):
            return
        h, r = node.height, node.round
        sent = self.fault.vote(self.sim, node.name, kind, h, r, bid, list(self.names))
        msgs: Dict[Optional[str], dict] = {}
        groups: Dict[Optional[str], List[str]] = defaultdict(list)
        msg_id = f"{kind}:{h}:{r}:{node.name}"
        for dst in self.names:
            if dst not in sent:
                continue
            b = sent[dst]
            if b not in msgs:
                m = {"type": kind, "height": h, "round": r, "block_id": b, "sender": node.name,
                     "sig": self.keys.sign(node.name, vote_digest(self.cfg, kind, h, r, b))}
                if kind == PRECOMMIT and b is not None and b in node.bodies:
                    m["block"] = node.bodies[b]
                msgs[b] = m
            groups[b].append(dst)
            self._send(node.name, dst, msgs[b], msg_id, t)
        self._log_sent(EventKind(kind), node, groups, bid, t)

    def send_catch_up(self, node: Validator, peer: str, height: int, rnd: int, t: float) -> None:
        """Answer a peer still at `height` with every block and proof from there on."""
        key = (peer, height, rnd)
        if key in node.synced or height > len(node.chain) or not self.up(node.name, t):
            return
        node.synced.add(key)
        entries = [({k: v for k, v in e.items() if k != "hash"}, p)
                   for e, p in zip(node.chain[height - 1:], node.proofs[height - 1:])]
        self.stats["catch_up"] += 1
        self._send(node.name, peer, {"type": "commit", "entries": entries, "sender": node.name},
                   f"commit:{height}:{rnd}:{node.name}", t)

    # ---------- log ----------
    def _log_sent(self, kind: EventKind, node: Validator, groups: Dict, honest, t: float, **extra) -> None:
        h, r = node.height, node.round
        for value, dsts in groups.items():
            data = {"height": h, "round": r, "block": value, **extra}
            if len(dsts) < len(self.names):
                data["to"] = dsts
            self.log.append(t, kind, node.actor, data)
        withheld = [d for d in self.names if not any(d in g for g in groups.values())]
        if set(groups) != {honest} or withheld:
            self.log.append(t, EventKind.FAULT_ACTION, node.actor,
                            {"action": kind.value, "height": h, "round": r, "honest": honest,
                             "sent": {str(v): d for v, d in groups.items()}, "withheld": withheld}, "truth")

    def log_timeout(self, node: Validator, t: float) -> None:
        self.log.append(t, EventKind.ROUND_TIMEOUT, node.actor,
                        {"height": node.height, "round": node.round, "step": node.step})

    def log_evidence(self, node: Validator, offender: str, kind: str, r: int, a, b) -> None:
        self.log.append(self.kernel.now, EventKind.EVIDENCE, node.actor,
                        {"offender": offender, "kind": kind, "height": node.height, "round": r, "blocks": [a, b]})

    def decided(self, node: Validator, header: dict, proof: dict, via: str, t: float) -> None:
        h = header["height"]
        self.decisions[h][node.name] = {"block": header["hash"], "round": proof["round"], "t": t, "via": via}
        self.log.append(t, EventKind.COMMIT, node.actor,
                        {"height": h, "round": proof["round"], "block": header["hash"], "via": via,
                         "sigs": proof["sigs"]})
        if self.on_decide is not None:
            self.on_decide(node, header, proof, t)

    # ---------- results ----------
    def honest(self) -> List[str]:
        bad = self.fault.compromised(self.sim)
        return [n for n in self.names if n not in bad]

    def conflicting_heights(self, nodes: Optional[Iterable[str]] = None) -> List[int]:
        """Heights at which two different blocks were committed (by any node, or among `nodes`).

        Every commit carries a valid commit proof, so a conflict means a
        substation could be shown two different decisions for one height."""
        keep = set(self.names if nodes is None else nodes)
        return sorted(h for h, d in self.decisions.items()
                      if len({c["block"] for n, c in d.items() if n in keep}) > 1)

    def first_commits(self) -> Dict[int, dict]:
        """Per height: the earliest commit and its latency from the height's first round-0 start."""
        out = {}
        for h, d in sorted(self.decisions.items()):
            first = min(d.values(), key=lambda c: c["t"])
            start = min(self.starts[h].values()) if self.starts.get(h) else None
            out[h] = {**first, "round": min(c["round"] for c in d.values()),
                      "latency": None if start is None else first["t"] - start}
        return out

    def undecided_heights(self, nodes: Optional[Iterable[str]] = None) -> List[int]:
        """Heights that started but that some node (of `nodes`, default all) never committed."""
        keep = list(self.names if nodes is None else nodes)
        return sorted(h for h in self.starts if any(n not in self.decisions.get(h, {}) for n in keep))


def run_committee(cfg, fault: Optional[Fault] = None, run: int = 0,
                  partitions: Iterable[Tuple[float, float, Iterable[Iterable[str]]]] = (),
                  net: Optional[Network] = None, heights: Optional[int] = None,
                  drain_s: Optional[float] = None, **kw) -> Committee:
    """One committee on its own, without the grid: for tests, the theory check and exp1-exp3.

    heights: run that many heights (dispatch slots) and then drain_s more
    seconds (default one dispatch period) so the last ones can finish; otherwise
    run cfg.horizon_s. partitions: (start, end, groups) windows, see network.py."""
    if heights is not None:
        drain = cfg.dispatch_period_s if drain_s is None else drain_s
        cfg = cfg.with_(horizon_h=(heights * cfg.dispatch_period_s + drain) / 3600.0)
    fault = fault if fault is not None else Honest()
    net = net if net is not None else Network(cfg, run)
    for start, end, groups in partitions:
        net.add_partition(start, end, groups)
    kernel = Kernel(cfg.horizon_s)
    log = EventLog(run_meta={"config": cfg.to_dict(), "run": run, "fault": fault.describe()})
    com = Committee(cfg, kernel, net, log=log, fault=fault, **kw)
    if heights is not None:
        com.n_slots = heights
    fault.begin_run(com, random.Random(f"fault-{cfg.seed}-{run}"))
    com.start(0.0)
    kernel.run()
    return com
