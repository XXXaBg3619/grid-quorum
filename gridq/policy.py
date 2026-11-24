"""Allocation policy: the government's rules as pure functions (new in gridq).

Principles kept from evsim/allocation.py (BlokcChin@796b43c):
  * pure and deterministic: same inputs, same output, so every validator
    computes the same decision and anyone can recompute it from the log;
  * order-independent: feeders are sorted by objective fields (weight,
    outage time, name) before any order-sensitive step;
  * one policy for all four architectures: A and B's centres, C and D's
    committee and B's pre-signed table all call decide().

Inputs of a decision for dispatch slot k:
  view         each substation's newest state report (verifier.Verifier.report:
               version, last executed seq, and per feeder its state, when it last
               switched, its finished outages and the length of the last one)
  measurement  power.Island.measure(k): the supply read at the slot's start
               and each feeder's planning demand for the slot (public forecast)
Durations are compared in whole minutes (TIME_STEP_S; substations book finished
outages in whole minutes too, verifier.outage_s), so a command that arrived a
few seconds later (a committee needs a few hundred milliseconds more than a
centre, a round lost to an offline proposer costs seconds, and substations hear
it at slightly different times) does not change the order of two feeders:
equal ones tie and are ordered by name.

The rule (allocate), with budget = supply x (1 - cfg.reserve_margin) and every
feeder's load estimated with the cold-load factor the physics uses
(power.cold_factor: a feeder being restored counts its pickup):
  1. critical feeders first, by weight (hospital, water, telecom, shelter),
     then by accumulated outage; each one that fits is on;
  2. non-critical feeders restored less than cfg.min_dwell_s ago stay on if
     they fit (no flapping);
  3. the other non-critical feeders by urgency = weight x accumulated outage,
     largest first, each one that fits is on: this is the rotation, since a
     feeder's urgency grows while it is off and stops growing while it is on;
  4. feeders shed less than cfg.min_dwell_s ago last, if anything is left.
Planned load never exceeds the budget.

Commands (commands_for) are the absolute targets that differ from the view,
by substation and feeder, numbered from the substation's last seq, with the
state version they assume and expires = slot start + cfg.command_ttl_s.

Two checks the architectures differ on (experiment 5):
  check_local    what B's substation enforcer can see: one command on its own.
                 Refuses cutting a critical feeder and more than
                 cfg.local_max_switch switchings per substation per dispatch
                 period (LocalEnforcer is the stateful Verifier.policy_fn).
  check_global   what C and D validators can see: the whole set of commands
                 against the island's state and supply. Refuses a set whose
                 planned load exceeds the budget, or that leaves a critical
                 feeder off while it would fit. C and D validators go further
                 and accept only the exact output of decide() (lasthop.PolicyDispatch);
                 check_global is the invariant every such output satisfies.

static_table() is B's pre-signed rotation table: decide() run in advance on the
forecast with a constant planned supply (cfg.plan_supply_share of peak demand).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .loads import FeederSpec
from .power import cold_factor
from .verifier import make_command, outage_s

EPS = 1e-6
TIME_STEP_S = 60.0          # durations are compared in whole minutes


def _q(x: float) -> float:
    return x if math.isinf(x) else round(x / TIME_STEP_S) * TIME_STEP_S


def start_view(specs: Iterable[FeederSpec]) -> Dict[str, dict]:
    """Every substation's state at the start of an island event: all feeders on, nothing switched."""
    out: Dict[str, dict] = {}
    for s in specs:
        r = out.setdefault(s.substation, {"substation": s.substation, "version": 0, "last_seq": 0, "states": {},
                                          "since": {}, "off_closed": {}, "last_off": {}})
        r["states"][s.name], r["since"][s.name] = "on", None
        r["off_closed"][s.name], r["last_off"][s.name] = 0.0, 0.0
    return {k: out[k] for k in sorted(out)}


def feeder_inputs(cfg, specs: Sequence[FeederSpec], view: Mapping[str, dict], measurement: Mapping,
                  slot: int) -> Dict[str, dict]:
    """Per feeder what allocate() needs, from the view and the slot's measurement."""
    t0 = slot * cfg.dispatch_period_s
    out = {}
    for spec in specs:
        r = view[spec.substation]
        f, st = spec.name, r["states"][spec.name]
        since = r["since"].get(f)
        if st == "off":
            off_s, on_s = _q(max(0.0, t0 - (since if since is not None else 0.0))), 0.0
        else:
            off_s, on_s = 0.0, (math.inf if since is None else _q(max(0.0, t0 - since)))
        out[f] = {"critical": spec.critical, "weight": spec.weight, "state": st, "off_s": off_s, "on_s": on_s,
                  "last_off": _q(float(r["last_off"].get(f, 0.0))),
                  "off_total": _q(float(r["off_closed"].get(f, 0.0))) + off_s,
                  "demand_kw": float(measurement["demand_kw"][f])}
    return out


def planned_kw(cfg, x: Mapping, on: bool = True) -> float:
    """A feeder's load if it is (kept or put) on in this slot, cold-load pickup included."""
    if not on:
        return 0.0
    if x["state"] == "on":
        return x["demand_kw"] * cold_factor(cfg, x["last_off"], x["on_s"])
    return x["demand_kw"] * cold_factor(cfg, x["off_s"], 0.0)


def allocate(cfg, feeders: Mapping[str, Mapping], supply_kw: float) -> Dict[str, str]:
    """Target state of every feeder for one slot (the rule in the module docstring)."""
    budget = max(0.0, supply_kw) * (1.0 - cfg.reserve_margin)
    targets = {f: "off" for f in feeders}
    used = 0.0

    def take(f: str) -> None:
        nonlocal used
        need = planned_kw(cfg, feeders[f])
        if used + need <= budget + EPS:
            targets[f], used = "on", used + need

    def urgency(f: str):
        x = feeders[f]
        return (-x["weight"] * x["off_total"], -x["weight"], f)

    names = sorted(feeders)
    for f in sorted((f for f in names if feeders[f]["critical"]),
                    key=lambda f: (-feeders[f]["weight"], -feeders[f]["off_total"], f)):
        take(f)
    rest = [f for f in names if not feeders[f]["critical"]]
    dwell = cfg.min_dwell_s
    held_on = [f for f in rest if feeders[f]["state"] == "on" and feeders[f]["on_s"] < dwell]
    held_off = [f for f in rest if feeders[f]["state"] == "off" and feeders[f]["off_s"] < dwell]
    free = [f for f in rest if f not in held_on and f not in held_off]
    for f in sorted(held_on, key=lambda f: (-feeders[f]["weight"], f)):
        take(f)
    for f in sorted(free, key=urgency):
        take(f)
    for f in sorted(held_off, key=urgency):
        take(f)
    return targets


def decide(cfg, specs: Sequence[FeederSpec], view: Mapping[str, dict], measurement: Mapping,
           slot: int) -> Dict[str, str]:
    return allocate(cfg, feeder_inputs(cfg, specs, view, measurement, slot), measurement["supply_kw"])


def commands_for(cfg, targets: Mapping[str, str], view: Mapping[str, dict], seq_base: Mapping[str, int],
                 slot: int) -> List[dict]:
    """Absolute commands for every feeder whose target differs from the view."""
    expires = slot * cfg.dispatch_period_s + cfg.command_ttl_s
    out = []
    for s in sorted(view):
        r, k = view[s], 0
        for f in sorted(r["states"]):
            if f in targets and targets[f] != r["states"][f]:
                out.append(make_command(s, f, targets[f], seq_base.get(s, 0) + k + 1, r["version"] + k, expires))
                k += 1
    return out


def advance_view(view: Mapping[str, dict], targets: Mapping[str, str], t: float) -> Dict[str, dict]:
    """The view after every target is executed at time t, bookkept as a verifier would (versions too)."""
    out = {}
    for s, r in view.items():
        r = {**r, "states": dict(r["states"]), "since": dict(r["since"]), "off_closed": dict(r["off_closed"]),
             "last_off": dict(r["last_off"])}
        for f in sorted(r["states"]):
            new = targets.get(f, r["states"][f])
            if new == r["states"][f]:
                continue
            if new == "on":
                spell = outage_s(t - (r["since"][f] if r["since"][f] is not None else 0.0))
                r["off_closed"][f] += spell
                r["last_off"][f] = spell
            r["states"][f], r["since"][f] = new, t
            r["version"] += 1
        out[s] = r
    return out


# ---------- checks ----------
def check_local(critical: bool, state: str, target: str, switches_this_period: int, cfg) -> str:
    """B's enforcer on one command: '' or the rule it breaks."""
    if target == state:
        return ""
    if target == "off" and critical:
        return "critical_feeder"
    if switches_this_period >= cfg.local_max_switch:
        return "too_many_switchings"
    return ""


class LocalEnforcer:
    """B's substation enforcer as Verifier.policy_fn(verifier, cmd, t): check_local plus its counter."""

    def __init__(self, cfg, specs: Iterable[FeederSpec]):
        self.cfg = cfg
        self.critical = {s.name for s in specs if s.critical}
        self.window: Dict[str, tuple] = {}          # substation -> (dispatch period, switchings in it)
        self.refused: Dict[str, int] = {}

    def __call__(self, verifier, cmd: Mapping, t: float) -> bool:
        f = cmd.get("feeder")
        period = int(math.floor(verifier.local_time(t) / self.cfg.dispatch_period_s + 1e-9))
        p, n = self.window.get(verifier.name, (period, 0))
        n = n if p == period else 0
        why = check_local(f in self.critical, verifier.feeders.get(f), cmd.get("target"), n, self.cfg)
        if why:
            self.refused[why] = self.refused.get(why, 0) + 1
            return False
        if cmd.get("target") != verifier.feeders.get(f):
            self.window[verifier.name] = (period, n + 1)
        return True


def check_global(cfg, specs: Sequence[FeederSpec], view: Mapping[str, dict], measurement: Mapping, slot: int,
                 commands: Iterable[Mapping]) -> str:
    """What a validator sees: the island after all the commands, against the slot's supply.
    '' or 'overload' (planned load above the budget) or 'critical_off' (a critical feeder off
    that would fit)."""
    xs = feeder_inputs(cfg, specs, view, measurement, slot)
    after = {f: x["state"] for f, x in xs.items()}
    for c in commands:
        if c.get("feeder") in after and c.get("target") in ("on", "off"):
            after[c["feeder"]] = c["target"]
    budget = max(0.0, measurement["supply_kw"]) * (1.0 - cfg.reserve_margin)
    load = sum(planned_kw(cfg, x, after[f] == "on") for f, x in xs.items())
    if load > budget + EPS:
        return "overload"
    for f, x in xs.items():
        if x["critical"] and after[f] == "off" and load + planned_kw(cfg, x) <= budget + EPS:
            return "critical_off"
    return ""


# ---------- B's pre-signed table ----------
def static_table(cfg, specs: Sequence[FeederSpec], loads, plan_kw: float, n_slots: int,
                 view: Optional[Mapping[str, dict]] = None) -> List[Dict[str, str]]:
    """Targets for every slot, decided in advance from the forecast and a constant supply plan_kw,
    each slot assuming the previous slots' targets were executed."""
    view = view if view is not None else start_view(specs)
    table = []
    for k in range(n_slots):
        t0 = k * cfg.dispatch_period_s
        meas = {"supply_kw": plan_kw, "demand_kw": {s.name: loads.slot_forecast(s.name, t0) for s in specs}}
        targets = decide(cfg, specs, view, meas, k)
        table.append(targets)
        view = advance_view(view, targets, t0)
    return table
