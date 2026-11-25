"""One live validator (architectures C and D): the simulator's Committee driven for the local node only.

What differs from the simulator, and why:
  LiveCommittee  every validator's name and public key but one local
                 Validator; messages go over UDP (to itself: at once); no slot
                 limit; catch-up answers cut to fit a datagram; and a guard
                 that never signs at or below the newest (height, round, step)
                 signed before, kept in a file, so a restarted validator cannot
                 double-sign by accident (CometBFT's priv_validator_state).
  catch-up       a validator that hears a height more than one ahead of its
                 own, or has just started, asks its peers for the blocks it
                 missed (catchup_request), because a restarted validator would
                 otherwise stay silent behind its guard and never be answered.
  echo           the first copy of each signed proposal for the current height
                 or later is passed on to the other validators once, standing
                 in for CometBFT's gossip. An equivocating proposer that sends
                 two blocks to two groups is then caught by everyone
                 (EVIDENCE); without it nobody sees both blocks.
  last hop       only blocks of the current or previous slot are sent to the
                 substations, so a validator catching up does not resend old
                 blocks (substations fetch missing heights themselves).

Dispatch is lasthop.RotatingDispatch (one group of feeders off per slot,
computed from the substations' signed reports on the chain); reports, the
report-inclusion floor and sync answers work as in lasthop.LastHop.

    GRIDQ_NAME=v0 python -m gridq.live.validator     # keys in /keys (GRIDQ_KEYS)
"""
from __future__ import annotations

import bisect
import json
import math
import os
import random
from collections import defaultdict
from typing import Dict, List, Mapping, Optional, Sequence

from ..consensus import Committee
from ..consensus.messages import PRECOMMIT, PREVOTE, block_id
from ..faults import Equivocate, Fault, Honest
from ..kernel import PRIO_MESSAGE
from ..lasthop import RotatingDispatch, default_feeders
from .runtime import (MAX_DATAGRAM, PORT, STATUS_PORT, LiveKernel, LiveLog, LiveNode, UdpTransport, env_config,
                      env_t0, json_reply, run_forever)

CATCH_UP_BYTES = MAX_DATAGRAM - 4000     # room left for the envelope
STEP_ORDER = {"proposal": 0, PREVOTE: 1, PRECOMMIT: 2}
RECENT_HEIGHTS = 40                      # heights whose hashes go into the status (conflict check)


def chain_entries(chain: Sequence[dict], proofs: Sequence[dict], start: int, limit_bytes: int = CATCH_UP_BYTES,
                  max_items: int = 1000) -> List[list]:
    """[block, proof] pairs from height `start` on, as many as fit in `limit_bytes` of JSON (at least one)."""
    out, size = [], 0
    for e, p in zip(chain[start - 1:start - 1 + max_items], proofs[start - 1:start - 1 + max_items]):
        item = [{k: v for k, v in e.items() if k != "hash"}, p]
        n = len(json.dumps(item, separators=(",", ":")))
        if out and size + n > limit_bytes:
            break
        out.append(item)
        size += n
    return out


class LiveCommittee(Committee):
    def __init__(self, cfg, kernel, transport, *, local: str, guard_path: Optional[str] = None, **kw):
        super().__init__(cfg, kernel, None, **kw)
        if local not in self.names:
            raise ValueError(f"{local} is not one of the validators {self.names}")
        self.local, self.transport, self.guard_path = local, transport, guard_path
        self.n_slots = 2 ** 62                       # no last slot: the demo runs until stopped
        self.signed = self._load_guard()             # newest (height, round, step) signed

    @property
    def node(self):
        return self.nodes[self.local]

    def start(self, t: float = 0.0) -> None:
        self.schedule_height(self.node, t)

    def _send(self, src: str, dst: str, msg: dict, msg_id: str, t: float) -> None:
        self.stats["sent"] += 1
        if dst == self.local:
            self.kernel.schedule(t, PRIO_MESSAGE, self._arrive, dst, msg)
        elif not self.transport.send(dst, msg):
            self.stats["lost"] += 1

    # ---------- double-sign guard ----------
    def _load_guard(self) -> tuple:
        if self.guard_path and os.path.exists(self.guard_path):
            with open(self.guard_path, encoding="utf-8") as fh:
                d = json.load(fh)
            return (int(d["height"]), int(d["round"]), int(d["step"]))
        return (0, -1, -1)

    def may_sign(self, height: int, rnd: int, step: str) -> bool:
        mark = (height, rnd, STEP_ORDER[step])
        if mark <= self.signed:
            self.stats["guard_refused"] += 1
            return False
        self.signed = mark
        if self.guard_path:
            tmp = self.guard_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"height": mark[0], "round": mark[1], "step": mark[2]}, fh)
            os.replace(tmp, self.guard_path)
        return True

    def send_proposal(self, node, block, vr, pol, t):
        if self.up(node.name, t) and self.may_sign(node.height, node.round, "proposal"):
            super().send_proposal(node, block, vr, pol, t)

    def send_vote(self, node, kind, bid, t):
        if self.up(node.name, t) and self.may_sign(node.height, node.round, kind):
            super().send_vote(node, kind, bid, t)

    # ---------- catch-up ----------
    def send_catch_up(self, node, peer, height, rnd, t):
        key = (peer, height, rnd)
        if key in node.synced or height > len(node.chain) or not self.up(node.name, t):
            return
        node.synced.add(key)
        self.catch_up(peer, height, t)

    def catch_up(self, peer: str, height: int, t: float) -> None:
        entries = chain_entries(self.node.chain, self.node.proofs, height)
        if entries:
            self.stats["catch_up"] += 1
            self._send(self.local, peer, {"type": "commit", "entries": entries, "sender": self.local},
                       f"commit:{height}:{self.local}", t)


class ValidatorNode(LiveNode):
    def __init__(self, cfg, name: str, *, names: Sequence[str], feeders: Mapping[str, Sequence[str]], keys,
                 sub_keys, transport: UdpTransport, kernel: LiveKernel, log: LiveLog,
                 fault: Optional[Fault] = None, guard_path: Optional[str] = None):
        super().__init__(kernel, transport, log)
        self.cfg, self.name = cfg, name
        self.fault = fault if fault is not None else Honest()
        self.subs = sorted(feeders)
        self.reports: Dict[str, Dict[str, dict]] = defaultdict(dict)
        self.report_log: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.dispatch = RotatingDispatch(cfg, feeders, sub_keys, groups=3, reports_of=lambda v: self.reports[v])
        self.dispatch.floor_of = self.report_floor
        self.com = LiveCommittee(cfg, kernel, transport, local=name, guard_path=guard_path, keys=keys, log=log,
                                 fault=self.fault, sim=self, names=names, propose_fn=self.dispatch.propose,
                                 valid_fn=self.dispatch.valid, on_decide=self.forward,
                                 prevote_fn=self.dispatch.includes_reports)
        self.node = self.com.node
        self.asked_at = -math.inf                    # last catchup_request sent
        self.answered: Dict[str, tuple] = {}         # peer -> (height, time) of the last catch-up answer
        self.echoed: set = set()

    # ---------- run ----------
    def start(self) -> None:
        self.fault.begin_run(self, random.Random(f"fault-{self.name}"))
        self.com.start(self.kernel.now)
        self.request_catch_up(force=True)

    def on_message(self, msg: dict) -> None:
        typ = msg.get("type")
        if typ in ("proposal", PREVOTE, PRECOMMIT, "commit"):
            h = msg.get("height")
            if type(h) is int and h > self.node.height + 1:
                self.request_catch_up()
            if typ == "proposal":
                self.echo(msg)
            self.com._arrive(self.name, msg)
        elif typ == "catchup_request":
            self.answer_catch_up(msg)
        elif typ == "report":
            self.on_report(msg.get("report"))
        elif typ == "sync_request":
            self.answer_sync(msg)

    def control(self, req: dict) -> None:
        mode = req.get("fault")
        if mode not in ("honest", "equivocate"):
            return super().control(req)
        self.fault = Equivocate([self.name]) if mode == "equivocate" else Honest()
        self.fault.begin_run(self, random.Random(f"fault-{self.name}-{self.kernel.now}"))
        self.com.fault = self.fault
        self.log.note(self.kernel.now, "control", f"fault mode is now {mode}")

    # ---------- validators ----------
    def echo(self, msg: dict) -> None:
        """Pass a signed proposal for this height or later on to the other validators, once."""
        h, sig = msg.get("height"), msg.get("sig")
        if type(h) is not int or h < self.node.height or sig in self.echoed or msg.get("sender") == self.name:
            return
        if not self.com.authentic(msg):
            return
        if len(self.echoed) > 10000:
            self.echoed.clear()
        self.echoed.add(sig)
        for v in self.com.names:
            if v not in (self.name, msg.get("sender")):
                self.transport.send(v, msg)

    def request_catch_up(self, force: bool = False) -> None:
        t = self.kernel.now
        if not force and t - self.asked_at < 1.0:
            return
        self.asked_at = t
        msg = {"type": "catchup_request", "height": self.node.height, "sender": self.name}
        for v in self.com.names:
            if v != self.name:
                self.transport.send(v, msg)

    def answer_catch_up(self, msg: dict) -> None:
        peer, h, t = msg.get("sender"), msg.get("height"), self.kernel.now
        if peer not in self.com.names or peer == self.name or type(h) is not int or not 1 <= h <= len(self.node.chain):
            return
        last = self.answered.get(peer)
        if last is not None and last[0] == h and t - last[1] < 1.0:
            return
        self.answered[peer] = (h, t)
        self.com.catch_up(peer, h, t)

    # ---------- substations ----------
    def on_report(self, r) -> None:
        """A substation's signed state report (as lasthop.LastHop._at_validator)."""
        cur = self.reports[self.name].get(r.get("substation")) if isinstance(r, dict) else None
        if self.dispatch.authentic(r) and (cur is None or r["version"] > cur["version"]):
            self.reports[self.name][r["substation"]] = r
            self.report_log[self.name][r["substation"]].append((self.kernel.now, r["version"]))

    def report_floor(self, validator: str, cutoff: float) -> Dict[str, int]:
        out = {}
        for s, entries in self.report_log[validator].items():
            i = bisect.bisect_right(entries, (cutoff, math.inf))
            if i:
                out[s] = entries[i - 1][1]
        return out

    def answer_sync(self, msg: dict) -> None:
        s, h, k = msg.get("substation"), msg.get("from"), msg.get("k")
        if s not in self.subs or type(h) is not int or not 1 <= h <= len(self.node.chain):
            return
        self.com.stats["sync_replies"] += 1
        self.transport.send(s, {"type": "sync", "entries": chain_entries(self.node.chain, self.node.proofs, h),
                                "sender": self.name, "delivery": f"sync:{s}:{k}:{self.name}"})

    def forward(self, node, header: dict, proof: dict, t: float) -> None:
        """on_decide: send a fresh block and its commit proof to every substation."""
        if header["slot"] < self.com.slot_of(t) - 1:
            return
        block = {k: v for k, v in header.items() if k != "hash"}
        h = header["height"]
        for s in self.subs:
            self.transport.send(s, {"type": "deliver", "block": block, "proof": proof, "sender": self.name,
                                    "delivery": f"deliver:{h}:{self.name}:{s}"})

    # ---------- status ----------
    def status(self) -> dict:
        n, t = self.node, self.kernel.now
        last = n.chain[-1] if n.chain else None
        lat = []
        for h in range(max(1, len(n.chain) - 9), len(n.chain) + 1):
            d, s = self.com.decisions.get(h, {}).get(self.name), self.com.starts.get(h, {}).get(self.name)
            if d is not None and s is not None and d["via"] == "votes":
                lat.append(round(d["t"] - s, 3))
        return {
            "role": "validator", "name": self.name, "t": round(t, 3), "slot": self.com.slot_of(t),
            "height": n.height, "round": n.round, "step": n.step, "fault": self.fault.label,
            "last": None if last is None else {
                "height": last["height"], "hash": last["hash"][:16], "slot": last["slot"],
                "proposer": last["proposer"], "round": self.node.proofs[-1]["round"],
                "commands": len(last["payload"].get("commands", ())),
                "reports": len(last["payload"].get("reports", ())),
                "t": round(self.com.decisions.get(last["height"], {}).get(self.name, {}).get("t", t), 3)},
            "hashes": {str(e["height"]): e["hash"][:16] for e in n.chain[-RECENT_HEIGHTS:]},
            "latency_s": lat, "evidence": self.log.evidence[-20:],
            "reports": {s: r["version"] for s, r in sorted(self.reports[self.name].items())},
            "guard": list(self.com.signed), "stats": dict(self.com.stats), "net": dict(self.transport.stats),
            "events": list(self.log.recent)[-15:],
        }

    def http_get(self, path: str, query: dict):
        if path == "/chain":
            frm = max(1, int(query.get("from", "1")))
            return json_reply(chain_entries(self.node.chain, self.node.proofs, frm, max_items=50))
        return super().http_get(path, query)


def main() -> None:
    from .keys import read_private, read_public, rings
    cfg = env_config()
    name = os.environ["GRIDQ_NAME"]
    keydir = os.environ.get("GRIDQ_KEYS", "/keys")
    names = [f"v{i}" for i in range(cfg.n_validators)]
    feeders = default_feeders(int(os.environ.get("GRIDQ_SUBSTATIONS", "4")))
    keys, sub_keys = rings(read_public(os.path.join(keydir, "public.json")),
                           {name: read_private(os.path.join(keydir, "self.key"))})
    t0 = env_t0()
    transport = UdpTransport(PORT, peers=[n for n in names if n != name] + sorted(feeders))
    kernel, log = LiveKernel(t0), LiveLog(name, t0)
    guard = os.environ.get("GRIDQ_GUARD", "/var/lib/gridq/guard.json")
    os.makedirs(os.path.dirname(guard), exist_ok=True)
    node = ValidatorNode(cfg, name, names=names, feeders=feeders, keys=keys, sub_keys=sub_keys,
                         transport=transport, kernel=kernel, log=log, guard_path=guard)
    log.note(kernel.now, "start", f"validator {name} up: n={cfg.n_validators}, f={cfg.f}, Q={cfg.quorum}, "
                                  f"slot {cfg.dispatch_period_s:g} s, holds keys {keys.holds}, last signed {node.com.signed}")
    run_forever(node, STATUS_PORT)


if __name__ == "__main__":
    main()
