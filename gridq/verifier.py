"""Substation verifier: the light node at the last hop (design doc, questions 25, 26, 28).

The trust boundary is the last hop: a substation executes a command only if it
can check by itself that the current validator set committed it, for this
substation, in order, recently, and on the state the substation is in now.
Nothing a relay adds is trusted, so a relay (a hacked dispatch centre, a
forwarder, someone on the link) can delay or drop commands but cannot forge,
reorder or replay them.

A substation keeps: the last header it trusts (height and hash, from genesis),
the last command sequence number it executed, its state version (+1 for every
executed command; local protective actions will add to it once power.py
exists) and its feeders' states.

Commands are absolute ("set feeder F3 off"), never toggles, and carry
  substation, feeder, target ("on" / "off"), seq (per substation, increasing),
  state_version (the substation's version the decision was based on) and
  expires (start of the decision's dispatch slot + cfg.command_ttl_s).
In C and D they sit in a block's payload: the block binds them to chain,
epoch and height, and the commit proof (Q precommit signatures) authenticates
them. In A and B the dispatch centre signs each command (sign_command).

Checks in this order; the first one that fails is the reason logged in
COMMAND_REJECTED (REJECT_REASONS):

  binding    wrong_chain, wrong_epoch: not this island's chain or not the
             fixed validator set (epoch changes are future work)
  order      stale_seq: the block's height is not above the trusted header.
             A copy of the trusted header itself is dropped as a duplicate,
             without an event: every validator sends every block
  signature  insufficient_quorum (C, D): not Q distinct validators' precommits
             for this block (consensus.verify_commit); bad_signature (A, B):
             the centre's signature does not verify. Always on.
  order      a valid block more than one height ahead waits until the gap is
             filled (lasthop.Substation fetches it); header_mismatch: the next
             block does not extend the trusted header (chain.verify_chain)
  then, per command addressed to this substation, in the block's order:
             unknown_feeder: no such feeder here (always checked: there is no
             breaker to operate)
  target     relative_target: not an absolute state
  order      stale_seq: seq not above the last executed one
  version    state_mismatch: state_version is not the current version
  ttl        expired: past `expires` by the substation's own clock; secondary,
             because clocks can be jammed (experiment 4)
             policy_violation: B only, the local rule check refused it

Experiment 4 turns the named groups off (`checks`) to show which check stops
which replay. The signature check stays on: without it nothing is verified.

Fail-safe (question 26): without a fresh valid command the substation simply
holds its state; only local protection (overload, UFLS) may act, which comes
with power.py. No automatic re-close and no switch to a fallback schedule,
except in B, whose static plan is its definition.
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Callable, Iterable, List, Mapping, Optional

from .chain import GENESIS, verify_chain
from .consensus.messages import verify_commit
from .events import EventKind, EventLog
from .hashing import canonical_hash

REJECT_REASONS = (
    "wrong_chain", "wrong_epoch", "stale_seq", "unknown_feeder", "relative_target",
    "state_mismatch", "insufficient_quorum", "bad_signature", "header_mismatch",
    "expired", "policy_violation",
)
TARGETS = ("on", "off")
CHECKS = ("binding", "order", "target", "version", "ttl")
ALL_CHECKS = frozenset(CHECKS)


def make_command(substation: str, feeder: str, target: str, seq: int, state_version: int,
                 expires: float) -> dict:
    return {"substation": substation, "feeder": feeder, "target": target, "seq": seq,
            "state_version": state_version, "expires": expires}


def command_digest(chain_id, epoch, cmd: dict) -> str:
    """What the dispatch centre signs in A and B: the command bound to chain and epoch."""
    return canonical_hash({"type": "command", "chain_id": chain_id, "epoch": epoch, "command": cmd})


def sign_command(cfg, keys, centre: str, cmd: dict) -> dict:
    """A, B: a command as the dispatch centre sends it."""
    return {"chain_id": cfg.chain_id, "epoch": cfg.epoch, "issuer": centre, "command": cmd,
            "sig": keys.sign(centre, command_digest(cfg.chain_id, cfg.epoch, cmd))}


def commands_for(block, substation: str) -> List[dict]:
    """The commands in a block's payload that are addressed to `substation`."""
    payload = block.get("payload") if isinstance(block, dict) else None
    cmds = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(cmds, list):
        return []
    return [c for c in cmds if isinstance(c, dict) and c.get("substation") == substation]


class Verifier:
    """One substation's light node: what it trusts, its feeders and its checks.

    keys: the validators' keys (C, D) or the key ring holding the centre's key
    (A, B, with `centre` naming it). clock_offset_s: how far the substation's
    clock is ahead of true time (negative: behind). policy_fn(verifier, cmd):
    B's local rule check."""

    def __init__(self, cfg, name: str, feeders: Mapping[str, str], *, keys=None,
                 centre: Optional[str] = None, checks: Iterable[str] = ALL_CHECKS,
                 clock_offset_s: float = 0.0, policy_fn: Optional[Callable] = None,
                 log: Optional[EventLog] = None):
        checks = frozenset(checks)
        if not checks <= ALL_CHECKS:
            raise ValueError(f"unknown checks {sorted(checks - ALL_CHECKS)}; known: {CHECKS}")
        if any(s not in TARGETS for s in feeders.values()):
            raise ValueError("feeder states must be 'on' or 'off'")
        self.cfg, self.name, self.keys, self.centre = cfg, name, keys, centre
        self.actor = f"substation:{name}"
        self.checks = checks
        self.clock_offset_s = clock_offset_s
        self.policy_fn = policy_fn
        self.log = log if log is not None else EventLog()
        self.feeders = dict(feeders)
        self.tip_height, self.tip_hash = 0, GENESIS
        self.last_seq = 0
        self.version = 0
        self.stats: Counter = Counter()

    def local_time(self, t: float) -> float:
        return t + self.clock_offset_s

    # ---------- C, D: committed blocks ----------
    def on_block(self, block, proof, t: float, delivery: str = "", src: str = "") -> str:
        """Check a block and its commit proof, then run this substation's commands in it.

        Returns "applied", "rejected", "duplicate" (a copy of the trusted
        header) or "gap" (valid, but more than one height ahead)."""
        cmds = commands_for(block, self.name)
        h = block.get("height") if isinstance(block, dict) else None
        ctx = {"substation": self.name, "height": h, "delivery": delivery, "from": src}
        if "binding" in self.checks:
            if not isinstance(block, dict) or block.get("chain_id") != self.cfg.chain_id:
                return self._reject(cmds, "wrong_chain", ctx, t)
            if block.get("epoch") != self.cfg.epoch:
                return self._reject(cmds, "wrong_epoch", ctx, t)
        if not isinstance(h, int) or isinstance(h, bool):
            return self._reject(cmds, "insufficient_quorum", ctx, t)
        if "order" in self.checks and h <= self.tip_height:
            if h == self.tip_height and isinstance(proof, dict) and proof.get("block") == self.tip_hash:
                self.stats["duplicate"] += 1
                return "duplicate"
            return self._reject(cmds, "stale_seq", ctx, t)
        if not self._committed(block, proof):
            return self._reject(cmds, "insufficient_quorum", ctx, t)
        if "order" in self.checks:
            if h > self.tip_height + 1:
                self.stats["gap"] += 1
                return "gap"
            if not verify_chain([dict(block, hash=proof["block"])], prev=self.tip_hash)["ok"]:
                return self._reject(cmds, "header_mismatch", ctx, t)
            self.tip_height, self.tip_hash = h, proof["block"]
        self.stats["blocks"] += 1
        for c in cmds:
            self._execute(c, ctx, t)
        return "applied"

    def _committed(self, block, proof) -> bool:
        if "binding" in self.checks:
            return verify_commit(self.keys, self.cfg, block, proof) == ""
        # Without the binding check, count signatures against the chain and epoch the
        # block itself names, as a gateway that only counts signatures would.
        named = SimpleNamespace(chain_id=block.get("chain_id"), epoch=block.get("epoch"),
                                quorum=self.cfg.quorum)
        return verify_commit(self.keys, named, block, proof) == ""

    # ---------- A, B: commands signed by the dispatch centre ----------
    def on_command(self, envelope, t: float, delivery: str = "", src: str = "") -> str:
        """Check one centre-signed command and execute it. Returns "applied" or "rejected"."""
        cmd = envelope.get("command") if isinstance(envelope, dict) else None
        cmds = [cmd] if isinstance(cmd, dict) else []
        ctx = {"substation": self.name, "height": None, "delivery": delivery, "from": src}
        if not cmds:
            return self._reject(cmds, "bad_signature", ctx, t)
        if "binding" in self.checks:
            if envelope.get("chain_id") != self.cfg.chain_id:
                return self._reject(cmds, "wrong_chain", ctx, t)
            if envelope.get("epoch") != self.cfg.epoch:
                return self._reject(cmds, "wrong_epoch", ctx, t)
        digest = command_digest(envelope.get("chain_id"), envelope.get("epoch"), cmd)
        if (self.centre is None or envelope.get("issuer") != self.centre
                or not self.keys.verify(self.centre, digest, envelope.get("sig"))):
            return self._reject(cmds, "bad_signature", ctx, t)
        if cmd.get("substation") != self.name:
            return self._reject(cmds, "unknown_feeder", ctx, t)
        return "applied" if self._execute(cmd, ctx, t) else "rejected"

    # ---------- one command ----------
    def _execute(self, cmd: dict, ctx: dict, t: float) -> bool:
        f, target, seq = cmd.get("feeder"), cmd.get("target"), cmd.get("seq")
        expires = cmd.get("expires")
        if f not in self.feeders:
            reason = "unknown_feeder"
        elif "target" in self.checks and target not in TARGETS:
            reason = "relative_target"
        elif "order" in self.checks and not (type(seq) is int and seq > self.last_seq):
            reason = "stale_seq"
        elif "version" in self.checks and cmd.get("state_version") != self.version:
            reason = "state_mismatch"
        elif "ttl" in self.checks and not (type(expires) in (int, float) and self.local_time(t) <= expires):
            reason = "expired"
        elif self.policy_fn is not None and not self.policy_fn(self, cmd):
            reason = "policy_violation"
        else:
            reason = ""
        if reason:
            self._log_reject(cmd, reason, ctx, t)
            return False
        old = self.feeders[f]
        new = target if target in TARGETS else ("off" if old == "on" else "on")   # a toggle flips
        self.feeders[f] = new
        self.version += 1
        if type(seq) is int:
            self.last_seq = max(self.last_seq, seq)
        self.stats["executed"] += 1
        self.log.append(t, EventKind.COMMAND_ACCEPTED, self.actor,
                        {**ctx, "feeder": f, "target": target, "seq": seq, "switched": new != old})
        if new != old:
            self.log.append(t, EventKind.FEEDER_SWITCHED, self.actor,
                            {"substation": self.name, "feeder": f, "state": new, "cause": "command",
                             "delivery": ctx["delivery"]})
        return True

    def _reject(self, cmds: List[dict], reason: str, ctx: dict, t: float) -> str:
        self.stats[reason] += 1
        for c in cmds:
            self._log_reject(c, reason, ctx, t)
        return "rejected"

    def _log_reject(self, cmd: dict, reason: str, ctx: dict, t: float) -> None:
        self.stats["rejected_commands"] += 1
        self.log.append(t, EventKind.COMMAND_REJECTED, self.actor,
                        {**ctx, "feeder": cmd.get("feeder"), "target": cmd.get("target"),
                         "seq": cmd.get("seq"), "reason": reason})
