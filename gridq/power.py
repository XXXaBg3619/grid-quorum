"""Simplified island power model (new in gridq).

Only what the design doc keeps. This is a distributed-systems project: no power
flow and no protection transients; protective relays keep acting locally as
they do today.

  supply       G(t), the generation still available inside the island
               (Supply, from cfg.supply_profile scaled by the island's peak
               demand): steps when lines are cut, slow ramps as it recovers
  feeders      loads.FeederSpec: load parts, critical flag, priority weight,
               UFLS stage; on or off as the substations' breakers are
  cold load    a feeder re-energised after an outage of length d draws
               1 + (cfg.cold_load_multiplier - 1) x min(1, d / cfg.cold_load_decay_s)
               times its load, falling back to 1 linearly over
               cfg.cold_load_decay_s (cold_factor; the policy uses the same
               formula to plan)
  balance      served load against G(t), checked after every switching or
               supply change and every cfg.balance_check_s. Served load above
               G(t) x (1 + cfg.ufls_margin) for cfg.ufls_delay_s trips
               simplified under-frequency load shedding (UFLS): whole stages in
               a fixed order (stage 1 first, critical feeders exempt) until the
               load fits G(t). The stages do not know the government's rules,
               so UFLS can cut the feeders the rotation had just restored.
  collapse     if shedding every stage is not enough, the island blacks out:
               every feeder opens, the island is dark for cfg.blackstart_s
               (measured supply 0, nothing can be closed: interlock
               "dead_bus"), then the supply returns and the dispatch has to
               restore feeders again, with cold load
  state        UFLS trips and the collapse are local actions at the
               substations (Verifier.local_action), so they bump the state
               version and a command computed before them is refused

Island is the physical truth of one run. It learns about switching from the
substations' verifiers (on_switch), switches feeders only through them, and
logs SUPPLY_CHANGED, UFLS_TRIP and ISLAND_COLLAPSE. Decision makers read only
measure(): the measured supply and the load forecast for a dispatch slot.

Metrics read from the log: critical-load hours, UFLS trips, island collapses.
"""
from __future__ import annotations

import bisect
import random
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .events import EventKind, EventLog
from .kernel import PRIO_PHYSICS, Kernel
from .loads import HOUR, FeederSpec, Loads

EPS = 1e-6


def cold_factor(cfg, off_s: float, on_s: float) -> float:
    """Load multiplier of a feeder re-energised on_s seconds ago after an outage of off_s seconds."""
    d = cfg.cold_load_decay_s
    if off_s <= 0.0 or d <= 0.0 or on_s >= d:
        return 1.0
    amp = (cfg.cold_load_multiplier - 1.0) * min(1.0, off_s / d)
    return 1.0 + amp * (1.0 - max(0.0, on_s) / d)


@dataclass(frozen=True)
class Supply:
    """G(t) in kW: points (t, kW), linear in between; two points at one time are a step."""
    points: Tuple[Tuple[float, float], ...]

    def __post_init__(self):
        ts = [t for t, _ in self.points]
        if not self.points or ts[0] != 0.0 or ts != sorted(ts):
            raise ValueError("supply points must be sorted by time and start at 0")

    def value(self, t: float) -> float:
        ts = [p[0] for p in self.points]
        i = bisect.bisect_right(ts, t)
        if i == 0:
            return self.points[0][1]
        if i == len(self.points):
            return self.points[-1][1]
        (t0, v0), (t1, v1) = self.points[i - 1], self.points[i]
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    def strike(self) -> float:
        """The first instant drop of G(t) (two points at one time, the second lower): the strike."""
        for (t0, v0), (t1, v1) in zip(self.points, self.points[1:]):
            if t0 == t1 and v1 < v0:
                return t0
        raise ValueError("the supply scenario has no strike")

    def breakpoints(self) -> List[Tuple[float, float, Optional[Tuple[float, float]]]]:
        """(t, value from t on, (t_next, value_next) if a ramp follows) for every distinct point time."""
        out = []
        times = sorted({t for t, _ in self.points})
        for t in times:
            later = [p for p in self.points if p[0] > t]
            v = self.value(t)
            ramp = (later[0][0], later[0][1]) if later and abs(later[0][1] - v) > EPS else None
            out.append((t, v, ramp))
        return out


def supply_scenario(cfg, peak_kw: float, run: int = 0, profile: Optional[Sequence] = None) -> Supply:
    """cfg.supply_profile (hour, share of peak demand) scaled to kW; every point after hour 0
    moves by one keyed draw in [-cfg.supply_jitter_h, cfg.supply_jitter_h] hours."""
    profile = list(profile if profile is not None else cfg.supply_profile)
    rng = random.Random(f"supply-{cfg.seed}-{run}")
    shift = rng.uniform(-cfg.supply_jitter_h, cfg.supply_jitter_h) * HOUR if cfg.supply_jitter_h > 0 else 0.0
    pts = [(0.0, profile[0][1] * peak_kw)]
    for h, share in profile[1:]:
        pts.append((max(0.0, h * HOUR + shift), share * peak_kw))
    return Supply(tuple((round(t, 3), round(v, 3)) for t, v in pts))


class Island:
    """The physical island of one run: feeder states, loads, G(t), UFLS and collapse."""

    def __init__(self, cfg, kernel: Kernel, log: EventLog, specs: Sequence[FeederSpec], loads: Loads,
                 supply: Supply):
        self.cfg, self.kernel, self.log = cfg, kernel, log
        self.specs: Dict[str, FeederSpec] = {s.name: s for s in specs}
        self.loads, self.supply = loads, supply
        self.state = {f: "on" for f in self.specs}
        self.off_at: Dict[str, Optional[float]] = {f: None for f in self.specs}
        self.restored_at: Dict[str, Optional[float]] = {f: None for f in self.specs}
        self.last_off: Dict[str, float] = {f: 0.0 for f in self.specs}
        self.stages = sorted({s.ufls_stage for s in specs if s.ufls_stage > 0})
        self.subs: Mapping = {}
        self.dark_intervals: List[List[float]] = []
        self.overload_since: Optional[float] = None
        self._check_at, self._check_pending = -1.0, False
        self.stats: Counter = Counter()

    def attach(self, substations: Mapping) -> None:
        """The substations (name -> object with local_action(feeder, target, t, cause))."""
        self.subs = substations

    # ---------- time ----------
    def start(self, t0: float = 0.0) -> None:
        for t, v, ramp in self.supply.breakpoints():
            if t >= t0 - EPS:
                self.kernel.schedule(max(t, t0), PRIO_PHYSICS, self._supply_point, v, ramp)
        self.kernel.schedule(t0, PRIO_PHYSICS, self._tick)

    def _tick(self) -> None:
        self._check()
        self.kernel.after(self.cfg.balance_check_s, PRIO_PHYSICS, self._tick)

    def _supply_point(self, v: float, ramp) -> None:
        t = self.kernel.now
        data = {"supply_kw": round(v, 3), "cause": "scenario"}
        if ramp is not None:
            data["ramp_to"] = [ramp[0], ramp[1]]
        if self.dark(t):
            data["dark"] = True
        self.log.append(t, EventKind.SUPPLY_CHANGED, "sim", data)
        self.poke()

    def poke(self) -> None:
        """Check the balance right after the current event; switchings at one time share one check."""
        t = self.kernel.now
        if not (self._check_pending and abs(self._check_at - t) <= EPS):
            self._check_at, self._check_pending = t, True
            self.kernel.schedule(t, PRIO_PHYSICS, self._poked)

    def _poked(self) -> None:
        self._check_pending = False
        self._check()

    # ---------- state ----------
    def dark(self, t: float) -> bool:
        return any(a <= t + EPS and t < b - EPS for a, b in self.dark_intervals)

    def measured_supply(self, t: float) -> float:
        """What a decision maker reads as available supply at t: 0 while the island is dark."""
        return 0.0 if self.dark(t) else self.supply.value(t)

    def load_kw(self, f: str, t: float) -> float:
        if self.state[f] != "on":
            return 0.0
        r = self.restored_at[f]
        factor = 1.0 if r is None else cold_factor(self.cfg, self.last_off[f], t - r)
        return self.loads.demand(f, t) * factor

    def served_kw(self, t: float) -> float:
        return 0.0 if self.dark(t) else sum(self.load_kw(f, t) for f in self.specs)

    def measure(self, slot: int) -> dict:
        """The measurements of dispatch slot k, the same for every decision maker at any later time:
        supply read at the slot's start and each feeder's planning demand for the slot."""
        t0 = slot * self.cfg.dispatch_period_s
        return {"supply_kw": round(self.measured_supply(t0), 1),
                "demand_kw": {f: round(self.loads.slot_forecast(f, t0), 1) for f in sorted(self.specs)}}

    # ---------- hooks from the substations ----------
    def switched(self, verifier, feeder: str, old: str, new: str, t: float, cause: str) -> None:
        """Verifier.on_switch: a breaker moved."""
        if feeder not in self.state:
            return
        if new == "off":
            self.off_at[feeder] = t
        else:
            off = self.off_at[feeder]
            self.last_off[feeder] = 0.0 if off is None else t - off
            self.restored_at[feeder] = t
        self.state[feeder] = new
        self.poke()

    def interlock(self, verifier, cmd: dict, t: float) -> str:
        """Verifier.interlock_fn: nothing can be closed while the island is dark."""
        return "dead_bus" if cmd.get("target") == "on" and self.dark(t) else ""

    # ---------- balance, UFLS, collapse ----------
    def _check(self) -> None:
        t = self.kernel.now
        if self.dark(t):
            self.overload_since = None
            return
        g = self.supply.value(t)
        if self.served_kw(t) <= g * (1.0 + self.cfg.ufls_margin) + EPS:
            self.overload_since = None
            return
        if self.overload_since is None:
            self.overload_since = t
            self.kernel.schedule(t + self.cfg.ufls_delay_s, PRIO_PHYSICS, self._check)
            return
        if t - self.overload_since < self.cfg.ufls_delay_s - EPS:
            return
        self._ufls(t, g)

    def _ufls(self, t: float, g: float) -> None:
        self.overload_since = None
        load = self.served_kw(t)
        for stage in self.stages:
            on = [f for f in sorted(self.specs) if self.specs[f].ufls_stage == stage and self.state[f] == "on"]
            if not on:
                continue
            before = load
            for f in on:
                self.subs[self.specs[f].substation].local_action(f, "off", t, "ufls")
            load = self.served_kw(t)
            self.stats["ufls_trips"] += 1
            self.log.append(t, EventKind.UFLS_TRIP, "sim",
                            {"stage": stage, "feeders": on, "supply_kw": round(g, 1),
                             "served_kw_before": round(before, 1), "served_kw_after": round(load, 1)})
            if load <= g + EPS:
                return
        if load > g * (1.0 + self.cfg.ufls_margin) + EPS:
            self._collapse(t, g, load)

    def _collapse(self, t: float, g: float, load: float) -> None:
        self.stats["collapses"] += 1
        end = t + self.cfg.blackstart_s
        self.log.append(t, EventKind.ISLAND_COLLAPSE, "sim",
                        {"supply_kw": round(g, 1), "served_kw": round(load, 1), "dark_until": round(end, 3)})
        for f in sorted(self.specs):
            if self.state[f] == "on":
                self.subs[self.specs[f].substation].local_action(f, "off", t, "collapse")
        self.dark_intervals.append([t, end])
        self.log.append(t, EventKind.SUPPLY_CHANGED, "sim", {"supply_kw": 0.0, "cause": "collapse"})
        self.kernel.schedule(end, PRIO_PHYSICS, self._blackstart)

    def _blackstart(self) -> None:
        t = self.kernel.now
        self.log.append(t, EventKind.SUPPLY_CHANGED, "sim",
                        {"supply_kw": round(self.supply.value(t), 3), "cause": "blackstart"})
        self.poke()
