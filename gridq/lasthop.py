"""The last hop in C and D: committed blocks out to the substations, state reports back (new in gridq).

  delivery   every validator that commits a block sends it with its commit
             proof to every substation, so one honest validator's copy is
             enough when up to f validators withhold. Anyone who copies a
             delivery can send it again later: experiment 4.
  ordering   a substation applies blocks in height order (verifier.Verifier);
             it keeps a valid block that is more than one height ahead and
             asks every validator for the missing heights (substation.py).
  reports    a substation signs its state (Verifier.report) with its own key
             and sends it to every validator shortly after the state changes
             and once per dispatch period. A proposer puts each substation's
             newest report on the chain, so the next commands are computed
             from agreed state, every validator can check them, and a
             substation that missed or refused commands, or whose protection
             tripped feeders, gets commands for the state it is really in.
  dispatch   what a block carries and how every validator checks it
             (ChainDispatch): PolicyDispatch runs policy.decide on the reports
             and the slot's measurements (architectures C and D in sim.py);
             RotatingDispatch, one group of feeders off per dispatch slot
             without a power model, is what experiment 4 and the tests use.

Every validator-to-substation message passes fault.last_hop (which may record
or drop it) and then the network; LastHop.inject() is how an attacker on the
link sends. run_lasthop() builds one island (committee, substations, network)
without the power model, for the tests and experiment 4.
"""
from __future__ import annotations

import bisect
import math
import random
from collections import Counter, defaultdict
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .consensus import Committee
from .crypto import KeyRing
from .events import EventKind, EventLog
from .faults import Fault, Honest
from .kernel import PRIO_MESSAGE, Kernel
from .loads import FeederSpec, by_substation
from .network import Network
from .policy import commands_for, decide, start_view
from .substation import (FUTURE_CAP, REPORT_DELAY_S, SYNC_RETRY_MAX_S, SYNC_RETRY_S,  # noqa: F401 (re-exported)
                         Link, Substation, report_digest, valid_report)
from .verifier import ALL_CHECKS, Verifier, make_command

SYNC_BATCH = 64             # most blocks in one sync reply


def default_feeders(n_substations: int = 4, per_substation: int = 3) -> Dict[str, List[str]]:
    """Substations S1.. with feeders S1-F1, S1-F2, ..."""
    return {f"S{i + 1}": [f"S{i + 1}-F{j + 1}" for j in range(per_substation)] for i in range(n_substations)}


class ChainDispatch:
    """What C and D's committee decides, and how every validator checks a proposal.

    A block's payload is {"reports", *EXTRA, "commands"}:
      reports   the newest signed state report the proposer holds for each
                substation, if newer than the chain's view; at most one per
                substation, sorted by substation
      EXTRA     inputs besides the reports that every validator reads alike
                (extras(slot); PolicyDispatch: the slot's measurements)
      commands  what the rules give for the chain's view updated by the
                reports and the extras (block_commands); seq continues the
                substation's count of commands on the chain, state_version is
                the view's version plus the command's place among the
                substation's commands in this block, expires = start of the
                block's slot + cfg.command_ttl_s
    valid() recomputes the commands, so a block is valid only if they are
    exactly what the rules give for the agreed state (the global check).

    A proposer may still leave a fresh report out: the block is then valid for
    the older state. includes_reports() is every validator's own check before it
    prevotes (Committee prevote_fn): a new proposal must carry, for every
    substation, a report at least as new as the newest one this validator got
    cfg.report_inclusion_s before the block's slot started (floor_of, set by
    LastHop). Without it, a compromised proposer can get decisions for a stale
    state committed (experiment 1).
    """
    EXTRA: Tuple[str, ...] = ()
    FULL_REPORTS = False        # reports must carry policy.decide's bookkeeping (substation.valid_report)

    def __init__(self, cfg, feeders: Mapping[str, Sequence[str]], sub_keys: KeyRing,
                 reports_of: Optional[Callable[[str], Mapping[str, dict]]] = None):
        self.cfg, self.sub_keys = cfg, sub_keys
        self.feeders = {s: list(fs) for s, fs in sorted(feeders.items())}
        self.reports_of = reports_of if reports_of is not None else (lambda node: {})
        self._seen: Dict[str, tuple] = {}          # node -> (chain length, view, commands per substation)
        self._checked: Dict[str, tuple] = {}       # node -> (chain length, {id(block): (block, valid)})
        self.floor_of: Optional[Callable[[str, float], Mapping[str, int]]] = None   # (validator, time) -> versions

    # ---------- what a subclass defines ----------
    def start_state(self) -> Dict[str, dict]:
        """The view before any report is on the chain."""
        raise NotImplementedError

    def entry(self, report: dict) -> dict:
        """What the view keeps of a report."""
        return {"version": report["version"], "states": report["states"]}

    def extras(self, slot: int) -> dict:
        return {}

    def block_commands(self, view: Mapping[str, dict], counts: Mapping[str, int], slot: int,
                       extras: Mapping) -> List[dict]:
        raise NotImplementedError

    # ---------- shared ----------
    def chain_state(self, node) -> Tuple[Dict[str, dict], Counter]:
        """(view, commands per substation) after the node's decided chain."""
        n, view, counts = self._seen.get(node.name, (0, None, None))
        if view is None:
            view, counts = self.start_state(), Counter()
        if n < len(node.chain):
            view, counts = dict(view), Counter(counts)
            for header in node.chain[n:]:
                payload = header.get("payload") or {}
                for r in payload.get("reports", ()):
                    view[r["substation"]] = self.entry(r)
                for c in payload.get("commands", ()):
                    counts[c["substation"]] += 1
            self._seen[node.name] = (len(node.chain), view, counts)
        return view, counts

    def authentic(self, r) -> bool:
        """A state report signed by a known substation, naming exactly its feeders."""
        return valid_report(self.cfg, self.sub_keys, self.feeders, r, self.FULL_REPORTS)

    def _updated(self, view: Mapping[str, dict], reports: Iterable[dict]) -> Dict[str, dict]:
        view = dict(view)
        for r in reports:
            view[r["substation"]] = self.entry(r)
        return view

    def propose(self, node, height: int, rnd: int, t: float) -> dict:
        view, counts = self.chain_state(node)
        mine = self.reports_of(node.name)
        reports = [mine[s] for s in sorted(mine) if s in view and mine[s]["version"] > view[s]["version"]]
        slot = node.com.slot_of(t)
        extras = self.extras(slot)
        return {"reports": reports, **extras,
                "commands": self.block_commands(self._updated(view, reports), counts, slot, extras)}

    def valid(self, node, block) -> bool:
        """The application check (Committee valid_fn); remembered per node and height."""
        n, seen = self._checked.get(node.name, (-1, None))
        if n != len(node.chain):
            seen = {}
            self._checked[node.name] = (len(node.chain), seen)
        hit = seen.get(id(block))
        if hit is None or hit[0] is not block:
            hit = seen[id(block)] = (block, self._valid(node, block))
        return hit[1]

    def includes_reports(self, node, block) -> bool:
        """Committee prevote_fn: no substation's state that this validator has known for a while is left out."""
        if self.floor_of is None or not math.isfinite(self.cfg.report_inclusion_s):
            return True
        payload = block.get("payload") if isinstance(block, dict) else None
        if not isinstance(payload, dict):
            return False
        view, _ = self.chain_state(node)
        have = {r.get("substation"): r.get("version") for r in payload.get("reports") or () if isinstance(r, dict)}
        cutoff = block["slot"] * self.cfg.dispatch_period_s - self.cfg.report_inclusion_s
        return all(max(have.get(s, -1), view[s]["version"]) >= v
                   for s, v in self.floor_of(node.name, cutoff).items() if s in view)

    def _valid(self, node, block) -> bool:
        payload = block.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"reports", "commands", *self.EXTRA}:
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
        extras = {k: payload[k] for k in self.EXTRA}
        if extras != self.extras(block["slot"]):
            return False
        return payload["commands"] == self.block_commands(self._updated(view, reports), counts, block["slot"], extras)


class RotatingDispatch(ChainDispatch):
    """Rotating outages written as absolute feeder targets, without a power model (experiment 4, tests).

    Feeder j of every substation is in group j % groups; in dispatch slot k,
    group k % groups is off and every other feeder is on. The view keeps each
    substation's version and feeder states (at first: every feeder on,
    version 0)."""

    def __init__(self, cfg, feeders: Mapping[str, Sequence[str]], sub_keys: KeyRing, groups: int = 3,
                 reports_of: Optional[Callable[[str], Mapping[str, dict]]] = None):
        if groups < 1:
            raise ValueError("groups must be at least 1")
        super().__init__(cfg, feeders, sub_keys, reports_of)
        self.groups = groups

    def start_state(self) -> Dict[str, dict]:
        return {s: {"version": 0, "states": {f: "on" for f in fs}} for s, fs in self.feeders.items()}

    def target(self, slot: int) -> Dict[str, Dict[str, str]]:
        off = slot % self.groups
        return {s: {f: "off" if j % self.groups == off else "on" for j, f in enumerate(fs)}
                for s, fs in self.feeders.items()}

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

    def block_commands(self, view, counts, slot, extras) -> List[dict]:
        return self.commands(view, counts, slot)


class PolicyDispatch(ChainDispatch):
    """policy.decide on the agreed state: C and D's dispatch.

    EXTRA "measurements": the slot's measured supply and planning demand
    (measure(slot), power.Island.measure). Every validator reads the same
    values, a simplification: real validators would each read the island's
    measurements and must agree on one. A block with other measurements is
    invalid. The view keeps each substation's whole report but the signature
    (policy.feeder_inputs reads version, last_seq and the per-feeder
    bookkeeping); commands are policy.commands_for the targets that
    policy.decide gives for the view and the measurements."""
    EXTRA = ("measurements",)
    FULL_REPORTS = True

    def __init__(self, cfg, specs: Sequence[FeederSpec], sub_keys: KeyRing, measure: Callable[[int], dict],
                 reports_of: Optional[Callable[[str], Mapping[str, dict]]] = None):
        self.specs = list(specs)
        super().__init__(cfg, by_substation(self.specs), sub_keys, reports_of)
        self.measure = measure
        self._start = start_view(self.specs)

    def start_state(self) -> Dict[str, dict]:
        return self._start

    def entry(self, report: dict) -> dict:
        return {k: v for k, v in report.items() if k != "sig"}

    def extras(self, slot: int) -> dict:
        return {"measurements": self.measure(slot)}

    def block_commands(self, view, counts, slot, extras) -> List[dict]:
        targets = decide(self.cfg, self.specs, view, extras["measurements"], slot)
        return commands_for(self.cfg, targets, view, counts, slot)


class LastHop(Link):
    """One island's decision path in C and D: the committee, its substations and the network between them.

    dispatch(sub_keys, reports_of) -> ChainDispatch builds what the committee
    decides (default: RotatingDispatch with `groups`); verifier_kw are extra
    Verifier arguments (sim.py: the island's interlock and switch hooks).
    Also the `sim` that fault hooks receive: hop.com is the committee, hop.subs
    the substations, hop.inject() sends as an attacker."""

    def __init__(self, cfg, kernel: Kernel, net: Network, *, feeders: Mapping[str, Sequence[str]],
                 log: Optional[EventLog] = None, fault: Optional[Fault] = None,
                 checks: Iterable[str] = ALL_CHECKS,
                 clock_offset_s: Union[float, Mapping[str, float]] = 0.0, groups: int = 3,
                 keys: Optional[KeyRing] = None,
                 dispatch: Optional[Callable[[KeyRing, Callable], ChainDispatch]] = None,
                 verifier_kw: Optional[Mapping] = None):
        self.cfg, self.kernel, self.net = cfg, kernel, net
        self.log = log if log is not None else EventLog()
        self.fault = fault if fault is not None else Honest()
        checks = frozenset(checks)
        self.sub_keys = KeyRing(sorted(feeders), cfg.seed)       # substations' own keys, apart from validators'
        self.reports: Dict[str, Dict[str, dict]] = defaultdict(dict)   # validator -> substation -> newest report
        self.report_log: Dict[str, Dict[str, List[Tuple[float, int]]]] = defaultdict(lambda: defaultdict(list))
        reports_of = lambda v: self.reports[v]  # noqa: E731
        self.dispatch = (dispatch(self.sub_keys, reports_of) if dispatch is not None else
                         RotatingDispatch(cfg, feeders, self.sub_keys, groups, reports_of=reports_of))
        self.dispatch.floor_of = self.report_floor
        self.com = Committee(cfg, kernel, net, keys=keys, log=self.log, fault=self.fault, sim=self,
                             propose_fn=self.dispatch.propose, valid_fn=self.dispatch.valid,
                             on_decide=self.forward, prevote_fn=self.dispatch.includes_reports)
        if set(feeders) & set(self.com.names):
            raise ValueError("substation and validator names must differ")
        offsets = clock_offset_s if isinstance(clock_offset_s, Mapping) else {s: clock_offset_s for s in feeders}
        start = self.dispatch.start_state()
        self.subs: Dict[str, Substation] = {
            s: Substation(self, Verifier(cfg, s, dict(start[s]["states"]), keys=self.com.keys, checks=checks,
                                         clock_offset_s=offsets.get(s, 0.0), log=self.log, **(verifier_kw or {})))
            for s in sorted(feeders)}
        self.issued: set = set()
        self.stats: Counter = Counter()

    # ---------- time ----------
    def start(self, t: float = 0.0) -> None:
        self.com.start(t)
        for s in self.subs.values():
            self.timer(t + self.cfg.dispatch_period_s / 2.0, s.heartbeat)

    # ---------- validators -> substations ----------
    def forward(self, node, header: dict, proof: dict, t: float) -> None:
        """on_decide: a validator committed a block; it sends block and proof to every substation."""
        h = header["height"]
        if h not in self.issued:
            self.issued.add(h)
            payload = header["payload"]
            cmds = payload.get("commands", ())
            data = {"height": h, "slot": header["slot"], "commands": len(cmds), "reports": len(payload["reports"])}
            if "measurements" in payload:
                data["supply_kw"] = payload["measurements"]["supply_kw"]
            self.log.append(t, EventKind.DECISION, "committee", data)
            for c in cmds:
                self.log.append(t, EventKind.COMMAND_ISSUED, "committee", {"height": h, **c})
        block = {k: v for k, v in header.items() if k != "hash"}
        for s in self.subs:
            msg = {"type": "deliver", "block": block, "proof": proof, "sender": node.name,
                   "delivery": f"deliver:{h}:{node.name}:{s}"}
            self.to_substation(node.name, s, msg, f"deliver:{h}:{node.name}", t)

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
                self.report_log[dst][r["substation"]].append((t, r["version"]))
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

    def report_floor(self, validator: str, cutoff: float) -> Dict[str, int]:
        """Per substation, the newest report version `validator` had received by time `cutoff`."""
        out = {}
        for s, entries in self.report_log[validator].items():
            i = bisect.bisect_right(entries, (cutoff, math.inf))
            if i:
                out[s] = entries[i - 1][1]
        return out

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
