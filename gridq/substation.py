"""A substation's end of the last hop, for every architecture (split out of lasthop.py).

  verifier   verifier.Verifier: what the substation trusts and its checks
  blocks     C, D: "deliver" and "sync" messages carry committed blocks with
             their commit proofs. A valid block more than one height ahead is
             kept and the gap requested from every validator, again after
             SYNC_RETRY_S, doubling up to SYNC_RETRY_MAX_S while it lasts.
  batches    A, B: once per dispatch slot the active centre sends a "batch":
             its signed commands for this substation (often none) and a signed
             "alive" token for the slot, so the substation knows the centre
             still decides even when nothing changes.
  fallback   B only (table given): after cfg.fallback_after_s without a fresh
             alive token from any centre, the substation runs its part of the
             pre-signed rotation table (policy.static_table) at every slot
             start, as local actions, until a centre is heard again.
  reports    the substation signs its state (Verifier.report) with its own key
             and sends it to the decision makers shortly after every change and
             once per dispatch period.
  decisions  every fresh decision the substation verified (a block applied, an
             alive token for a new slot) is logged as DECISION_APPLIED with its
             slot, for the availability metric.

The link (lasthop.LastHop for C and D, central.CentralPath for A and B) gives
cfg, sub_keys, log, timer(t, fn, *args), send_report(sub, report, k, t) and
request_sync(sub, from_height, k, t). Both links share Link: timers, the
fault's node_up, sending to a substation through fault.last_hop and the
network, and inject(), how an attacker on a substation's link sends.
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .events import EventKind
from .hashing import canonical_hash
from .kernel import PRIO_MESSAGE
from .verifier import TARGETS, Verifier

SYNC_RETRY_S = 5.0          # first wait before asking again for a missing height ...
SYNC_RETRY_MAX_S = 60.0     # ... doubling up to this
FUTURE_CAP = 64             # most blocks ahead of the trusted header a substation keeps
REPORT_DELAY_S = 0.5        # a change report waits this long, so one report covers one delivery's commands


def report_digest(cfg, report: dict) -> str:
    """What a substation signs in its state report: every field but the signature."""
    return canonical_hash({"type": "report", "chain_id": cfg.chain_id,
                           **{k: v for k, v in report.items() if k != "sig"}})


def valid_report(cfg, sub_keys, feeders: Mapping[str, Sequence[str]], r, full: bool = True) -> bool:
    """A state report signed by a known substation that names exactly its feeders. full: it also
    carries what policy.decide reads (last_seq, and since, off_closed, last_off per feeder)."""
    if not (isinstance(r, dict) and r.get("substation") in feeders and type(r.get("version")) is int
            and isinstance(r.get("states"), dict)):
        return False
    fs = set(feeders[r["substation"]])
    if set(r["states"]) != fs:
        return False
    if full:
        if type(r.get("last_seq")) is not int or any(st not in TARGETS for st in r["states"].values()):
            return False
        for key in ("since", "off_closed", "last_off"):
            d = r.get(key)
            if not isinstance(d, dict) or set(d) != fs:
                return False
            for x in d.values():
                if not ((isinstance(x, (int, float)) and not isinstance(x, bool)) or (key == "since" and x is None)):
                    return False
    return sub_keys.verify(r["substation"], report_digest(cfg, r), r.get("sig"))


def alive_digest(cfg, issuer: str, slot: int) -> str:
    """What a centre signs to say it decided dispatch slot `slot` (A, B)."""
    return canonical_hash({"type": "alive", "chain_id": cfg.chain_id, "epoch": cfg.epoch, "issuer": issuer,
                           "slot": slot})


class Substation:
    """A substation: its verifier, plus buffering, sync requests, centre batches, B's fallback and reports."""

    def __init__(self, link, verifier: Verifier, table: Optional[List[Mapping[str, str]]] = None):
        self.link, self.v, self.name = link, verifier, verifier.name
        self.future: Dict[int, tuple] = {}         # height -> (block, proof, delivery, sender) ahead of the tip
        self.sync_from, self.sync_wait, self.sync_next, self.n_sync = 0, SYNC_RETRY_S, 0.0, 0
        self.n_reports = 0
        self.report_pending = False
        self.table = table                         # B: pre-signed targets per slot (this substation's feeders)
        self.last_alive_slot, self.last_word = -1, 0.0
        self.fallback = False

    # backwards-compatible name used by lasthop and the tests
    @property
    def hop(self):
        return self.link

    # ---------- messages ----------
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
        elif typ == "batch":
            self.on_batch(msg, t)

    # ---------- C, D: blocks ----------
    def receive(self, block, proof, delivery: str, sender: str, t: float) -> None:
        before = self.v.version
        status = self.v.on_block(block, proof, t, delivery, sender)
        if status == "gap":
            if block["height"] not in self.future and len(self.future) < FUTURE_CAP:
                self.future[block["height"]] = (block, proof, delivery, sender)
            self.request_sync(t)
        elif status == "applied":
            self._applied(block, t)
            self._drain(t)
        if self.v.version != before:
            self.report_soon(t)

    def _applied(self, block: dict, t: float) -> None:
        self.link.log.append(t, EventKind.DECISION_APPLIED, self.v.actor,
                             {"substation": self.name, "slot": block.get("slot"), "height": block.get("height")})

    def _drain(self, t: float) -> None:
        while self.future:
            nxt = self.v.tip_height + 1
            for h in [h for h in self.future if h < nxt]:
                del self.future[h]
            item = self.future.pop(nxt, None)
            if item is None:
                break
            if self.v.on_block(item[0], item[1], t, item[2], item[3]) == "applied":
                self._applied(item[0], t)
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
        self.link.request_sync(self, nxt, self.n_sync, t)
        self.link.timer(self.sync_next, self._sync_check, nxt)

    def _sync_check(self, nxt: int, t: float) -> None:
        if self.v.tip_height < nxt and self.future:      # still missing, and a later block is known
            self.request_sync(t)

    # ---------- A, B: centre batches ----------
    def on_batch(self, msg: dict, t: float) -> None:
        before = self.v.version
        issuer, slot = msg.get("issuer"), msg.get("slot")
        if (issuer in self.v.centres and type(slot) is int and slot > self.last_alive_slot
                and self.v.keys.verify(issuer, alive_digest(self.v.cfg, issuer, slot), msg.get("alive"))):
            self.last_alive_slot, self.last_word = slot, t
            self.link.log.append(t, EventKind.DECISION_APPLIED, self.v.actor,
                                 {"substation": self.name, "slot": slot, "issuer": issuer})
            if self.fallback:
                self.fallback = False
                self.link.log.append(t, EventKind.FALLBACK_SCHEDULE, self.v.actor,
                                     {"substation": self.name, "action": "stop", "issuer": issuer})
        for env in msg.get("commands") or ():
            self.v.on_command(env, t, msg.get("delivery", ""), msg.get("sender", ""))
        if self.v.version != before:
            self.report_soon(t)

    # ---------- B: the pre-signed table ----------
    def slot_tick(self, t: float) -> None:
        """At every slot start by the local clock: enter or run the fallback table (B)."""
        if self.table is None:
            return
        period = self.v.cfg.dispatch_period_s
        k = int(math.floor(self.v.local_time(t) / period + 1e-9))
        if not self.fallback and t - self.last_word >= self.v.cfg.fallback_after_s - 1e-9:
            self.fallback = True
            self.link.log.append(t, EventKind.FALLBACK_SCHEDULE, self.v.actor,
                                 {"substation": self.name, "action": "start", "slot": k})
        if self.fallback and 0 <= k < len(self.table):
            changed = False
            for f in sorted(self.v.feeders):
                target = self.table[k].get(f)
                if target is not None and target != self.v.feeders[f]:
                    changed |= self.v.local_action(f, target, t, "table")
            if changed:
                self.report_soon(t)
        self.link.timer((k + 1) * period - self.v.clock_offset_s, self.slot_tick)

    # ---------- local actions ----------
    def local_action(self, feeder: str, target: str, t: float, cause: str) -> bool:
        changed = self.v.local_action(feeder, target, t, cause)
        if changed:
            self.report_soon(t)
        return changed

    # ---------- state reports ----------
    def report_soon(self, t: float) -> None:
        if not self.report_pending:
            self.report_pending = True
            self.link.timer(t + REPORT_DELAY_S, self._report_now)

    def _report_now(self, t: float) -> None:
        self.report_pending = False
        self.send_report(t)

    def heartbeat(self, t: float) -> None:
        self.send_report(t)
        self.link.timer(t + self.link.cfg.dispatch_period_s, self.heartbeat)

    def send_report(self, t: float) -> None:
        self.n_reports += 1
        r = self.v.report()
        r["sig"] = self.link.sub_keys.sign(self.name, report_digest(self.link.cfg, r))
        self.link.send_report(self, r, self.n_reports, t)


class Link:
    """What LastHop (C, D) and central.CentralPath (A, B) share. The subclass sets cfg, kernel, net,
    log, fault, subs (name -> Substation) and stats (a Counter)."""

    def timer(self, t: float, fn: Callable, *args) -> None:
        """Call fn(*args, now) at time t."""
        self.kernel.schedule(t, PRIO_MESSAGE, self._fire, fn, args)

    def _fire(self, fn: Callable, args: tuple) -> None:
        fn(*args, self.kernel.now)

    def up(self, node: str, t: float) -> bool:
        return self.fault.node_up(self, node, t)

    def to_substation(self, src: str, dst: str, msg: dict, msg_id: str, t: float) -> None:
        """A decision maker's message to substation dst: fault.last_hop may record or drop it."""
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
