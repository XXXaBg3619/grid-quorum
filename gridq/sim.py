"""Simulator: one island, one architecture, one run.

The same split as evsim's engine (BlokcChin@796b43c): the simulator owns time,
the log and the physical island; everything architecture-specific lives in an
Architecture (arch/), everything adversarial in a Fault (faults.py).

  Simulator(cfg, fault, run, specs=..., supply=..., partitions=..., checks=..., clock_offset_s=...)
    kernel   Kernel(cfg.horizon_s)            clock and queue                     kernel.py
    loads    Loads(cfg, specs, run)           feeder demand, public forecast      loads.py
    supply   supply_scenario(cfg, peak, run)  G(t)                                power.py
    net      Network(cfg, run)                delay, loss, partitions             network.py
    log      EventLog(run_meta=...)           hash-chained event log              events.py
    island   Island                           feeder states, cold load, UFLS      power.py
    arch     arch.get(cfg.arch)(self)         A / B / C / D decision path         arch/
    fault    Fault                            hooks the decision path calls       faults.py

  run():
    1. G(t) points and the balance check are scheduled (Island.start); the
       decision path starts (centres or committee, substation reports);
    2. at every dispatch slot the architecture decides target feeder states
       with policy.decide from the substations' signed state reports and
       measure(k), and sends them through its own path (centre-signed
       commands, or a block with a commit proof);
    3. substations verify (verifier.py), execute or hold, and report;
    4. the island checks the balance after every switching and supply change
       and trips UFLS stages or collapses (power.py);
    5. metrics.island_metrics reads the finished log.

All four architectures see the same loads, supply, network draws and fault
for a given (cfg.seed, run): common random numbers, so a difference between
them comes from the decision path.
"""
from __future__ import annotations

import random
from typing import Iterable, Mapping, Optional, Sequence, Tuple, Union

from . import arch as arches
from .events import EventLog
from .faults import Fault, Honest
from .kernel import Kernel
from .loads import FeederSpec, Loads, default_island, island_meta
from .network import Network
from .power import Island, Supply, supply_scenario
from .verifier import ALL_CHECKS


class Simulator:
    def __init__(self, cfg, fault: Optional[Fault] = None, run: int = 0, *,
                 specs: Optional[Sequence[FeederSpec]] = None, supply: Optional[Supply] = None,
                 load_scale: float = 1.0, net: Optional[Network] = None,
                 partitions: Iterable[Tuple[float, float, Iterable[Iterable[str]]]] = (),
                 checks: Iterable[str] = ALL_CHECKS, clock_offset_s: Union[float, Mapping[str, float]] = 0.0):
        self.cfg, self.run_no = cfg, run
        self.fault = fault if fault is not None else Honest()
        self.specs = list(specs) if specs is not None else default_island()
        self.checks, self.clock_offset_s = frozenset(checks), clock_offset_s
        self.kernel = Kernel(cfg.horizon_s)
        self.loads = Loads(cfg, self.specs, run, load_scale)
        self.peak_kw = self.loads.peak_kw()
        self.supply = supply if supply is not None else supply_scenario(cfg, self.peak_kw, run)
        self.net = net if net is not None else Network(cfg, run)
        for start, end, groups in partitions:
            self.net.add_partition(start, end, groups)
        self.log = EventLog(run_meta={
            "config": cfg.to_dict(), "run": run, "fault": self.fault.describe(), "island": island_meta(self.specs),
            "load_scale": load_scale, "peak_kw": round(self.peak_kw, 1),
            "supply": [list(p) for p in self.supply.points], "checks": sorted(self.checks),
            "clock_offset_s": clock_offset_s})
        self.island = Island(cfg, self.kernel, self.log, self.specs, self.loads, self.supply)
        self.arch = arches.get(cfg.arch)(self)
        self.link = self.arch.link
        self.island.attach(self.link.subs)

    def verifier_kw(self) -> dict:
        """What every substation verifier gets from the island: interlocks and the switch hook."""
        return {"interlock_fn": self.island.interlock, "on_switch": self.island.switched}

    def measure(self, slot: int) -> dict:
        """Dispatch slot k's measurements, the same for every decision maker (power.Island.measure)."""
        return self.island.measure(slot)

    def run(self) -> "Simulator":
        self.fault.begin_run(self.link, random.Random(f"fault-{self.cfg.seed}-{self.run_no}"))
        self.island.start(0.0)
        self.arch.start(0.0)
        self.kernel.run()
        self.arch.finish()
        self.log.run_meta["island_stats"] = dict(sorted(self.island.stats.items()))
        return self


def simulate(cfg, fault: Optional[Fault] = None, run: int = 0, **kw) -> Simulator:
    """Build and run one island event."""
    return Simulator(cfg, fault, run, **kw).run()
