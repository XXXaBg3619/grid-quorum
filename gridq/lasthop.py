"""The last hop in C and D: committed blocks out to the substations, state reports back (new in gridq).

  delivery   every validator that commits a block sends it with its commit
             proof to every substation, so one honest validator's copy is
             enough when up to f validators withhold. Anyone who copies a
             delivery can send it again later: experiment 4.
  ordering   a substation applies blocks in height order (verifier.Verifier).
             It keeps a valid block that is more than one height ahead and
             asks every validator for the missing heights, again after
             SYNC_RETRY_S, doubling up to SYNC_RETRY_MAX_S while the gap lasts.
  reports    a substation signs its state (version and feeder states) with
             its own key and sends it to every validator shortly after the
             state changes and once per dispatch period. A proposer puts each
             substation's newest report on the chain, so the next commands are
             computed from agreed state, every validator can check them, and a
             substation that missed or refused commands gets commands for the
             state it is really in.
  dispatch   RotatingDispatch stands in for policy.py until step 3: rotating
             outages, one group of feeders off per dispatch slot.

Every validator-to-substation message passes fault.last_hop (which may record
or drop it) and then the network; LastHop.inject() is how an attacker on the
link sends. run_lasthop() builds one island (committee, substations, network)
for the tests and experiment 4; sim.py will take over once the power model
exists.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .consensus import Committee
from .crypto import KeyRing
from .events import EventKind, EventLog
from .faults import Fault, Honest
from .hashing import canonical_hash
from .kernel import PRIO_MESSAGE, Kernel
from .network import Network
from .verifier import ALL_CHECKS, Verifier, make_command

SYNC_RETRY_S = 5.0          # first wait before asking again for a missing height ...
SYNC_RETRY_MAX_S = 60.0     # ... doubling up to this
SYNC_BATCH = 64             # most blocks in one sync reply
FUTURE_CAP = 64             # most blocks ahead of the trusted header a substation keeps
REPORT_DELAY_S = 0.5        # a change report waits this long, so one report covers one delivery's commands


def default_feeders(n_substations: int = 4, per_substation: int = 3) -> Dict[str, List[str]]:
    """Substations S1.. with feeders S1-F1, S1-F2, ..."""
    return {f"S{i + 1}": [f"S{i + 1}-F{j + 1}" for j in range(per_substation)] for i in range(n_substations)}


def report_digest(cfg, report: dict) -> str:
    """What a substation signs in its state report."""
    return canonical_hash({"type": "report", "chain_id": cfg.chain_id, "substation": report["substation"],
                           "version": report["version"], "states": report["states"]})


class RotatingDispatch:
    """Stand-in for policy.py (step 3): rotating outages written as absolute feeder targets.

    Feeder j of every substation is in group j % groups; in dispatch slot k,
    group k % groups is off and every other feeder is on. A block's commands
    follow from the chain and the block alone, so every validator checks them
    (valid):
      view      each substation's newest report on the chain (at first: every
                feeder on, version 0), updated by the reports in the block
      commands  one per feeder whose target in the block's slot differs from
                the view, by substation and feeder; seq continues the
                substation's count of commands on the chain; state_version is
                the view's version plus the command's place among the
                substation's commands in this block; expires = start of the
                block's slot + cfg.command_ttl_s
    """

    def __init__(self, cfg, feeders: Mapping[str, Sequence[str]], sub_keys: KeyRing, groups: int = 3,
                 reports_of: Optional[Callable[[str], Mapping[str, dict]]] = None):
        if groups < 1:
            raise ValueError("groups must be at least 1")
        self.cfg, self.groups, self.sub_keys = cfg, groups, sub_keys
        self.feeders = {s: list(fs) for s, fs in sorted(feeders.items())}
        self.reports_of = reports_of if reports_of is not None else (lambda node: {})
        self._seen: Dict[str, tuple] = {}          # node -> (chain length, view, commands per substation)

    def start_state(self) -> Dict[str, dict]:
        return {s: {"version": 0, "states": {f: "on" for f in fs}} for s, fs in self.feeders.items()}

    def target(self, slot: int) -> Dict[str, Dict[str, str]]:
        off = slot % self.groups
        return {s: {f: "off" if j % self.groups == off else "on" for j, f in enumerate(fs)}
                for s, fs in self.feeders.items()}

    def chain_state(self, node) -> Tuple[Dict[str, dict], Counter]:
        """(view, commands per substation) after the node's decided chain."""
        n, view, counts = self._seen.get(node.name, (0, self.start_state(), Counter()))
        if n < len(node.chain):
            view, counts = dict(view), Counter(counts)
            for header in node.chain[n:]:
                payload = header.get("payload") or {}
                for r in payload.get("reports", ()):
                    view[r["substation"]] = {"version": r["version"], "states": r["states"]}
                for c in payload.get("commands", ()):
                    counts[c["substation"]] += 1
            self._seen[node.name] = (len(node.chain), view, counts)
        return view, counts

    def commands(self, view: Mapping[str, dict], counts: Mapping[str, int], slot: int) -> List[dict]:
        targets = self.target(slot)
        expires = slot * self.cfg.dispatch_period_s + self.cfg.command_ttl_s
        out = []
        for s, fs in self.feeders.items():
            k = 0
            for f in fs:
                if targets[s][f] != view[s]["states"].get(f):
                    out.append(make_command(s, f, targets[s][f], counts.get(s, 0) + k + 1,
                                            view[s]["version"] + k, expires))
                    k += 1
        return out

    def authentic(self, r) -> bool:
        """A state report signed by a known substation, naming exactly its feeders."""
        if not (isinstance(r, dict) and r.get("substation") in self.feeders and type(r.get("version")) is int
                and isinstance(r.get("states"), dict)):
            return False
        return (set(r["states"]) == set(self.feeders[r["substation"]])
                and self.sub_keys.verify(r["substation"], report_digest(self.cfg, r), r.get("sig")))

    @staticmethod
    def _updated(view: Mapping[str, dict], reports: Iterable[dict]) -> Dict[str, dict]:
        view = dict(view)
        for r in reports:
            view[r["substation"]] = {"version": r["version"], "states": r["states"]}
        return view

    def propose(self, node, height: int, rnd: int, t: float) -> dict:
        view, counts = self.chain_state(node)
        mine = self.reports_of(node.name)
        reports = [mine[s] for s in sorted(mine) if s in view and mine[s]["version"] > view[s]["version"]]
        slot = node.com.slot_of(t)
        return {"reports": reports, "commands": self.commands(self._updated(view, reports), counts, slot)}

    def valid(self, node, block) -> bool:
        payload = block.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"reports", "commands"}:
            return False
        reports = payload["reports"]
        if not isinstance(reports, list) or not all(self.authentic(r) for r in reports):
            return False
        subs = [r["substation"] for r in reports]
        if subs != sorted(set(subs)):                    # at most one report per substation, in order
            return False
        view, counts = self.chain_state(node)
        if any(r["version"] <= view[r["substation"]]["version"] for r in reports):
            return False                                 # only newer reports: no rolling back a substation's state
        return payload["commands"] == self.commands(self._updated(view, reports), counts, block["slot"])


class Substation:
    """A substation's end of the last hop: its verifier, plus buffering, sync requests and state reports."""

    def __init__(self, hop: "LastHop", verifier: Verifier):
        self.hop, self.v, self.name = hop, verifier, verifier.name
        self.future: Dict[int, tuple] = {}         # height -> (block, proof, delivery, sender) ahead of the tip
        self.sync_from, self.sync_wait, self.sync_next, self.n_sync = 0, SYNC_RETRY_S, 0.0, 0
        self.n_reports = 0
        self.report_pending = False

    # ---------- blocks ----------
    def on_message(self, msg: dict, t: float) -> None:
        typ = msg.get("type")
        if typ == "deliver":
            self.receive(msg.get("block"), msg.get("proof"), msg.get("delivery", ""), msg.get("sender", ""), t)
        elif typ == "sync":
            for item in msg.get("entries") or ():
                if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], dict)):
                    continue
                h = item[0].get("height")
                if type(h) is int and h <= self.v.tip_height and "order" in self.v.checks:
                    self.v.stats["sync_known"] += 1     # replies overlap: heights already applied
                    continue
                self.receive(item[0], item[1], msg.get("delivery", ""), msg.get("sender", ""), t)

    def receive(self, block, proof, delivery: str, sender: str, t: float) -> None:
        before = self.v.version
        status = self.v.on_block(block, proof, t, delivery, sender)
        if status == "gap":
            if block["height"] not in self.future and len(self.future) < FUTURE_CAP:
                self.future[block["height"]] = (block, proof, delivery, sender)
            self.request_sync(t)
        elif status == "applied":
            self._drain(t)
        if self.v.version != before:
            self.report_soon(t)

    def _drain(self, t: float) -> None:
        while self.future:
            nxt = self.v.tip_height + 1
            for h in [h for h in self.future if h < nxt]:
                del self.future[h]
            item = self.future.pop(nxt, None)
            if item is None:
                break
            self.v.on_block(item[0], item[1], t, item[2], item[3])
            if self.v.tip_height < nxt:                  # rejected: wait for a good copy
                break
        if self.future:
            self.request_sync(t)

    def request_sync(self, t: float) -> None:
        nxt = self.v.tip_height + 1
        if nxt == self.sync_from and t < self.sync_next - 1e-9:
            return
        if nxt != self.sync_from:
            self.sync_from, self.sync_wait = nxt, SYNC_RETRY_S
        else:
            self.sync_wait = min(2.0 * self.sync_wait, SYNC_RETRY_MAX_S)
        self.sync_next = t + self.sync_wait
        self.n_sync += 1
        self.hop.request_sync(self, nxt, self.n_sync, t)
        self.hop.timer(self.sync_next, self._sync_check, nxt)

    def _sync_check(self, nxt: int, t: float) -> None:
        if self.v.tip_height < nxt and self.future:      # still missing, and a later block is known
            self.request_sync(t)

    # ---------- state reports ----------
    def report_soon(self, t: float) -> None:
        if not self.report_pending:
            self.report_pending = True
            self.hop.timer(t + REPORT_DELAY_S, self._report_now)

    def _report_now(self, t: float) -> None:
        self.report_pending = False
        self.send_report(t)

    def heartbeat(self, t: float) -> None:
        self.send_report(t)
        self.hop.timer(t + self.hop.cfg.dispatch_period_s, self.heartbeat)

    def send_report(self, t: float) -> None:
        self.n_reports += 1
        r = {"substation": self.name, "version": self.v.version, "states": dict(self.v.feeders)}
        r["sig"] = self.hop.sub_keys.sign(self.name, report_digest(self.hop.cfg, r))
        self.hop.send_report(self, r, self.n_reports, t)


class LastHop:
    """One island: the committee, its substations and the network between them.

    Also the `sim` that fault hooks receive: hop.com is the committee, hop.subs
    the substations, hop.inject() sends as an attacker."""

    def __init__(self, cfg, kernel: Kernel, net: Network, *, feeders: Mapping[str, Sequence[str]],
                 log: Optional[EventLog] = None, fault: Optional[Fault] = None,
                 checks: Iterable[str] = ALL_CHECKS,
                 clock_offset_s: Union[float, Mapping[str, float]] = 0.0, groups: int = 3,
                 keys: Optional[KeyRing] = None):
        self.cfg, self.kernel, self.net = cfg, kernel, net
        self.log = log if log is not None else EventLog()
        self.fault = fault if fault is not None else Honest()
        checks = frozenset(checks)
        self.sub_keys = KeyRing(sorted(feeders), cfg.seed)       # substations' own keys, apart from validators'
        self.reports: Dict[str, Dict[str, dict]] = defaultdict(dict)   # validator -> substation -> newest report
        self.dispatch = RotatingDispatch(cfg, feeders, self.sub_keys, groups, reports_of=lambda v: self.reports[v])
        self.com = Committee(cfg, kernel, net, keys=keys, log=self.log, fault=self.fault, sim=self,
                             propose_fn=self.dispatch.propose, valid_fn=self.dispatch.valid,
                             on_decide=self.forward)
        if set(feeders) & set(self.com.names):
            raise ValueError("substation and validator names must differ")
        offsets = clock_offset_s if isinstance(clock_offset_s, Mapping) else {s: clock_offset_s for s in feeders}
        self.subs: Dict[str, Substation] = {
            s: Substation(self, Verifier(cfg, s, {f: "on" for f in fs}, keys=self.com.keys, checks=checks,
                                         clock_offset_s=offsets.get(s, 0.0), log=self.log))
            for s, fs in sorted(feeders.items())}
        self.issued: set = set()
        self.stats: Counter = Counter()

    # ---------- time ----------
    def start(self, t: float = 0.0) -> None:
        self.com.start(t)
        for s in self.subs.values():
            self.timer(t + self.cfg.dispatch_period_s / 2.0, s.heartbeat)

    def timer(self, t: float, fn: Callable, *args) -> None:
        """Call fn(*args, now) at time t."""
        self.kernel.schedule(t, PRIO_MESSAGE, self._fire, fn, args)

    def _fire(self, fn: Callable, args: tuple) -> None:
        fn(*args, self.kernel.now)

    def up(self, node: str, t: float) -> bool:
        return self.fault.node_up(self, node, t)

    # ---------- validators -> substations ----------
    def forward(self, node, header: dict, proof: dict, t: float) -> None:
        """on_decide: a validator committed a block; it sends block and proof to every substation."""
        h = header["height"]
        if h not in self.issued:
            self.issued.add(h)
            for c in header["payload"].get("commands", ()):
                self.log.append(t, EventKind.COMMAND_ISSUED, "committee", {"height": h, **c})
        block = {k: v for k, v in header.items() if k != "hash"}
        for s in self.subs:
            msg = {"type": "deliver", "block": block, "proof": proof, "sender": node.name,
                   "delivery": f"deliver:{h}:{node.name}:{s}"}
            self.to_substation(node.name, s, msg, f"deliver:{h}:{node.name}", t)

    def to_substation(self, src: str, dst: str, msg: dict, msg_id: str, t: float) -> None:
        if not self.up(src, t):
            return
        self.stats["to_substations"] += 1
        if not self.fault.last_hop(self, src, dst, msg, t):
            self.stats["withheld"] += 1
            return
        arrival = self.net.deliver(msg_id, src, dst, t)
        if arrival is None:
            self.stats["lost"] += 1
        else:
            self.kernel.schedule(arrival, PRIO_MESSAGE, self._at_substation, dst, msg)

    def _at_substation(self, dst: str, msg: dict) -> None:
        t = self.kernel.now
        if self.up(dst, t):
            self.subs[dst].on_message(msg, t)
        else:
            self.stats["dropped_down"] += 1

    def inject(self, dst: str, msg: dict, t: float, info: Optional[dict] = None) -> None:
        """An attacker on substation dst's link sends msg at time t (never lost); logged as truth."""
        self.timer(t, self._inject_now, dst, msg, dict(info or {}))

    def _inject_now(self, dst: str, msg: dict, info: dict, t: float) -> None:
        self.stats["injected"] += 1
        self.log.append(t, EventKind.FAULT_ACTION, "attacker",
                        {"action": "inject", **info, "substation": dst, "delivery": msg.get("delivery")}, "truth")
        arrival = t + self.net.delay(f"inject:{msg.get('delivery')}", "attacker", dst)
        self.kernel.schedule(arrival, PRIO_MESSAGE, self._at_substation, dst, msg)

    # ---------- substations -> validators ----------
    def to_validator(self, src: str, dst: str, msg: dict, msg_id: str, t: float) -> None:
        if not self.up(src, t):
            return
        arrival = self.net.deliver(msg_id, src, dst, t)
        if arrival is None:
            self.stats["lost"] += 1
        else:
            self.kernel.schedule(arrival, PRIO_MESSAGE, self._at_validator, dst, msg)

    def _at_validator(self, dst: str, msg: dict) -> None:
        t = self.kernel.now
        if not self.up(dst, t):
            return
        if msg.get("type") == "report":
            r = msg.get("report")
            cur = self.reports[dst].get(r.get("substation")) if isinstance(r, dict) else None
            if self.dispatch.authentic(r) and (cur is None or r["version"] > cur["version"]):
                self.reports[dst][r["substation"]] = r
        elif msg.get("type") == "sync_request":
            self._answer_sync(dst, msg, t)

    def send_report(self, sub: Substation, report: dict, k: int, t: float) -> None:
        for v in self.com.names:
            self.to_validator(sub.name, v, {"type": "report", "report": report}, f"report:{sub.name}:{k}", t)

    def request_sync(self, sub: Substation, from_height: int, k: int, t: float) -> None:
        msg = {"type": "sync_request", "substation": sub.name, "from": from_height, "k": k}
        for v in self.com.names:
            self.to_validator(sub.name, v, msg, f"syncreq:{sub.name}:{k}", t)

    def _answer_sync(self, validator: str, msg: dict, t: float) -> None:
        node = self.com.nodes[validator]
        s, h, k = msg.get("substation"), msg.get("from"), msg.get("k")
        if s not in self.subs or type(h) is not int or not 1 <= h <= len(node.chain):
            return
        end = h - 1 + SYNC_BATCH
        entries = [({kk: vv for kk, vv in e.items() if kk != "hash"}, p)
                   for e, p in zip(node.chain[h - 1:end], node.proofs[h - 1:end])]
        self.stats["sync_replies"] += 1
        reply = {"type": "sync", "entries": entries, "sender": validator, "delivery": f"sync:{s}:{k}:{validator}"}
        self.to_substation(validator, s, reply, f"sync:{s}:{k}:{validator}", t)

    # ---------- results ----------
    def initial_states(self) -> Dict[str, Dict[str, str]]:
        return {s: dict(st["states"]) for s, st in self.dispatch.start_state().items()}


def run_lasthop(cfg, *, feeders: Optional[Mapping[str, Sequence[str]]] = None, fault: Optional[Fault] = None,
                run: int = 0, checks: Iterable[str] = ALL_CHECKS,
                clock_offset_s: Union[float, Mapping[str, float]] = 0.0, groups: int = 3,
                heights: Optional[int] = None, drain_s: Optional[float] = None,
                partitions: Iterable[Tuple[float, float, Iterable[Iterable[str]]]] = (),
                net: Optional[Network] = None) -> LastHop:
    """One island with its committee and substations (C, D), without the power model.

    heights: run that many dispatch slots, then drain_s more seconds (default
    one dispatch period); otherwise cfg.horizon_s. partitions: (start, end,
    groups) windows over validator and substation names (network.py)."""
    feeders = feeders if feeders is not None else default_feeders()
    checks = frozenset(checks)
    if heights is not None:
        drain = cfg.dispatch_period_s if drain_s is None else drain_s
        cfg = cfg.with_(horizon_h=(heights * cfg.dispatch_period_s + drain) / 3600.0)
    fault = fault if fault is not None else Honest()
    net = net if net is not None else Network(cfg, run)
    for start, end, sides in partitions:
        net.add_partition(start, end, sides)
    kernel = Kernel(cfg.horizon_s)
    log = EventLog(run_meta={"config": cfg.to_dict(), "run": run, "fault": fault.describe(),
                             "checks": sorted(checks), "clock_offset_s": clock_offset_s, "groups": groups})
    hop = LastHop(cfg, kernel, net, feeders=feeders, log=log, fault=fault, checks=checks,
                  clock_offset_s=clock_offset_s, groups=groups)
    if heights is not None:
        hop.com.n_slots = heights
    fault.begin_run(hop, random.Random(f"fault-{cfg.seed}-{run}"))
    hop.start(0.0)
    kernel.run()
    return hop
