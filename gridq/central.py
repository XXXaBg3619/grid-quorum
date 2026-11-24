"""A and B's decision path: a dispatch centre, its backup, and centre-signed commands (new in gridq).

  centres    "centre" (rank 0, active at the start) and "backup" (rank 1),
             each with its own key; substations accept either signature.
  failover   the active centre sends the other a heartbeat every
             cfg.heartbeat_s. A standby that hears none for cfg.failover_s
             takes over with the next term (FAILOVER, action "take_over"). An
             active centre that hears a heartbeat with a higher term, or the
             same term from a lower rank, steps down ("step_down"). A centre
             back from being down restarts as standby. A centre that is cut
             off but still running keeps deciding, so during a partition both
             centres can be active (split brain); substations then execute
             whichever signed command fits their state and refuse the rest.
  dispatch   at every slot start the active centre runs policy.decide on its
             newest state report from each substation (the start view if it
             has none) and the slot's measurements, numbers the commands
             from max(its own count, the reported last_seq) and sends each
             substation a "batch": its centre-signed commands and a signed
             alive token for the slot (substation.Substation.on_batch). A
             hacked centre changes the commands through
             fault.central_commands(sim, centre, slot, commands) (exp 5).
  reports    substations send their signed state reports to both centres.
  B          the same path with LocalEnforcer as every verifier's policy_fn
             and a pre-signed rotation table per substation
             (substation.Substation.slot_tick runs it after
             cfg.fallback_after_s without a fresh alive token).

The CentralPath is also the `sim` that fault hooks receive (node_up for the
centres and substations, last_hop for every batch, central_commands).
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from .crypto import KeyRing
from .events import EventKind, EventLog
from .faults import Fault, Honest
from .kernel import PRIO_DISPATCH, PRIO_MESSAGE, Kernel
from .loads import FeederSpec, by_substation
from .network import Network
from .policy import commands_for, decide, start_view
from .substation import Link, Substation, alive_digest, valid_report
from .verifier import ALL_CHECKS, Verifier, sign_command

CENTRES = ("centre", "backup")


class Centre:
    """One dispatch centre's state: active or standby, term, newest reports, commands issued."""

    def __init__(self, name: str, rank: int):
        self.name, self.rank = name, rank
        self.actor = f"centre:{name}"
        self.active = rank == 0
        self.term = 0
        self.last_heard = 0.0                    # last heartbeat from the other centre
        self.was_down = False
        self.reports: Dict[str, dict] = {}       # substation -> newest authentic report
        self.issued: Counter = Counter()         # substation -> highest seq this centre issued
        self.n_heartbeats = 0


class CentralPath(Link):
    """One island's decision path in A and B: two centres, the substations and the network between them."""

    def __init__(self, cfg, kernel: Kernel, net: Network, *, specs: Sequence[FeederSpec],
                 measure: Callable[[int], dict], log: Optional[EventLog] = None, fault: Optional[Fault] = None,
                 checks: Iterable[str] = ALL_CHECKS, clock_offset_s: Union[float, Mapping[str, float]] = 0.0,
                 verifier_kw: Optional[Mapping] = None, tables: Optional[Mapping[str, List[Mapping[str, str]]]] = None,
                 centres: Sequence[str] = CENTRES):
        self.cfg, self.kernel, self.net = cfg, kernel, net
        self.log = log if log is not None else EventLog()
        self.fault = fault if fault is not None else Honest()
        self.specs = list(specs)
        self.feeders = by_substation(self.specs)
        if set(self.feeders) & set(centres):
            raise ValueError("substation and centre names must differ")
        self.measure = measure
        self.keys = KeyRing(centres, cfg.seed)                   # the centres' keys
        self.sub_keys = KeyRing(sorted(self.feeders), cfg.seed)  # substations' own keys
        self.start_view = start_view(self.specs)
        self.centres: Dict[str, Centre] = {n: Centre(n, i) for i, n in enumerate(centres)}
        self.n_slots = cfg.n_dispatches
        offsets = clock_offset_s if isinstance(clock_offset_s, Mapping) else {s: clock_offset_s for s in self.feeders}
        tables = tables or {}
        self.subs: Dict[str, Substation] = {
            s: Substation(self, Verifier(cfg, s, dict(self.start_view[s]["states"]), keys=self.keys,
                                         centre=centres, checks=frozenset(checks),
                                         clock_offset_s=offsets.get(s, 0.0), log=self.log, **(verifier_kw or {})),
                          table=tables.get(s))
            for s in sorted(self.feeders)}
        self.stats: Counter = Counter()

    # ---------- time ----------
    def start(self, t: float = 0.0) -> None:
        for c in self.centres.values():
            self.timer(t, self._centre_tick, c)
        k = int(math.floor(t / self.cfg.dispatch_period_s + 1e-9))
        if k < self.n_slots:
            self.kernel.schedule(max(t, k * self.cfg.dispatch_period_s), PRIO_DISPATCH, self._dispatch, k)
        for s in self.subs.values():
            self.timer(t + self.cfg.dispatch_period_s / 2.0, s.heartbeat)
            if s.table is not None:
                self.timer(t, s.slot_tick)

    def _running(self, c: Centre, t: float) -> bool:
        """Whether centre c runs at t; a centre back from being down restarts as standby."""
        if not self.up(c.name, t):
            c.was_down = True
            return False
        if c.was_down:
            c.was_down, c.active, c.last_heard = False, False, t
            self.stats["restarts"] += 1
        return True

    # ---------- failover ----------
    def _centre_tick(self, c: Centre, t: float) -> None:
        if self._running(c, t):
            if c.active:
                self._heartbeat(c, t)
            elif t - c.last_heard >= self.cfg.failover_s - 1e-9:
                c.active, c.term = True, c.term + 1
                self.log.append(t, EventKind.FAILOVER, c.actor,
                                {"centre": c.name, "action": "take_over", "term": c.term,
                                 "silent_s": round(t - c.last_heard, 3)})
                self._heartbeat(c, t)
        self.timer(t + self.cfg.heartbeat_s, self._centre_tick, c)

    def _heartbeat(self, c: Centre, t: float) -> None:
        c.n_heartbeats += 1
        msg = {"type": "heartbeat", "sender": c.name, "term": c.term, "rank": c.rank}
        for other in self.centres:
            if other != c.name:
                self._to_centre(c.name, other, msg, f"heartbeat:{c.name}:{c.n_heartbeats}", t)

    def _on_heartbeat(self, c: Centre, msg: dict, t: float) -> None:
        sender, term = self.centres.get(msg.get("sender")), msg.get("term")
        if sender is None or sender is c or type(term) is not int:
            return
        c.last_heard = t
        if c.active and (term > c.term or (term == c.term and sender.rank < c.rank)):
            c.active = False
            self.log.append(t, EventKind.FAILOVER, c.actor,
                            {"centre": c.name, "action": "step_down", "term": term, "to": sender.name})
        c.term = max(c.term, term)

    # ---------- dispatch ----------
    def _dispatch(self, k: int) -> None:
        t = self.kernel.now
        for c in self.centres.values():
            if self._running(c, t) and c.active:
                self._decide(c, k, t)
        if k + 1 < self.n_slots:
            self.kernel.schedule((k + 1) * self.cfg.dispatch_period_s, PRIO_DISPATCH, self._dispatch, k + 1)

    def view(self, c: Centre) -> Dict[str, dict]:
        """Centre c's view: its newest report from each substation without the signature, else the start view."""
        return {s: ({kk: v for kk, v in c.reports[s].items() if kk != "sig"} if s in c.reports else self.start_view[s])
                for s in self.feeders}

    def _decide(self, c: Centre, k: int, t: float) -> None:
        view, meas = self.view(c), self.measure(k)
        targets = decide(self.cfg, self.specs, view, meas, k)
        seq_base = {s: max(c.issued[s], view[s]["last_seq"]) for s in view}
        cmds = self.fault.central_commands(self, c.name, k, commands_for(self.cfg, targets, view, seq_base, k))
        self.log.append(t, EventKind.DECISION, c.actor,
                        {"slot": k, "issuer": c.name, "term": c.term, "commands": len(cmds),
                         "reports": len(c.reports), "supply_kw": meas["supply_kw"]})
        by_sub: Dict[str, List[dict]] = {s: [] for s in self.subs}
        for cmd in cmds:
            self.log.append(t, EventKind.COMMAND_ISSUED, c.actor, {"issuer": c.name, "slot": k, **cmd})
            s, seq = cmd.get("substation"), cmd.get("seq")
            if type(seq) is int:
                c.issued[s] = max(c.issued[s], seq)
            if s in by_sub:
                by_sub[s].append(sign_command(self.cfg, self.keys, c.name, cmd))
        alive = self.keys.sign(c.name, alive_digest(self.cfg, c.name, k))
        for s, envs in by_sub.items():
            msg = {"type": "batch", "issuer": c.name, "slot": k, "alive": alive, "commands": envs,
                   "sender": c.name, "delivery": f"batch:{k}:{c.name}:{s}"}
            self.to_substation(c.name, s, msg, f"batch:{k}:{c.name}", t)

    # ---------- messages to the centres ----------
    def _to_centre(self, src: str, dst: str, msg: dict, msg_id: str, t: float) -> None:
        if not self.up(src, t):
            return
        arrival = self.net.deliver(msg_id, src, dst, t)
        if arrival is None:
            self.stats["lost"] += 1
        else:
            self.kernel.schedule(arrival, PRIO_MESSAGE, self._at_centre, dst, msg)

    def _at_centre(self, dst: str, msg: dict) -> None:
        t = self.kernel.now
        c = self.centres[dst]
        if not self._running(c, t):
            return
        if msg.get("type") == "heartbeat":
            self._on_heartbeat(c, msg, t)
        elif msg.get("type") == "report":
            r = msg.get("report")
            cur = c.reports.get(r.get("substation")) if isinstance(r, dict) else None
            if valid_report(self.cfg, self.sub_keys, self.feeders, r) and (cur is None or r["version"] > cur["version"]):
                c.reports[r["substation"]] = r

    def send_report(self, sub: Substation, report: dict, k: int, t: float) -> None:
        for c in self.centres:
            self._to_centre(sub.name, c, {"type": "report", "report": report}, f"report:{sub.name}:{k}", t)

    def request_sync(self, sub: Substation, from_height: int, k: int, t: float) -> None:
        """Only C and D's substations fetch blocks; never called here."""

    # ---------- results ----------
    def active_centres(self) -> List[str]:
        return [n for n, c in self.centres.items() if c.active]
