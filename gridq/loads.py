"""Synthetic feeder loads (new in gridq; evsim's EV workload is not ported).

The smallest unit the dispatch can switch is a feeder, not a customer (design
doc question 21): keeping the hospital on keeps the houses on the same feeder
on too. So a feeder is a mix of load parts, each a kind with a peak in kW:

  kind          critical  weight  daily shape (24 hourly factors, peak 1.0)
  hospital      yes       100     high all day, a little lower at night
  water         yes        90     pumping peaks in the morning and evening
  telecom       yes        80     almost flat
  shelter       yes        70     occupied mostly in the evening and at night
  residential   no          2     evening peak
  commercial    no          1     daytime plateau

A feeder is critical if it carries any critical part, and its weight is that
of its most important part. The shapes and the default island are illustrative
assumptions, not measured curves (the design doc lists the missing hospital,
water and telecom data as a limitation): results are a model check, and the
experiments sweep the shares and peaks instead of claiming one true island.

Actual demand is the shape times the peak times (1 + noise), the noise an AR(1)
process per feeder and minute drawn from a keyed RNG (seed, run, feeder), so
the honest and the faulty run of a pair and every architecture see the same
loads. forecast() is the same curve without noise: the public load forecast
every decision maker uses.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

HOUR = 3600.0


@dataclass(frozen=True)
class Kind:
    critical: bool
    weight: float
    shape: Tuple[float, ...]        # 24 factors, hour 0 to hour 23, linear in between


KINDS: Dict[str, Kind] = {
    "hospital": Kind(True, 100.0, (0.78, 0.76, 0.75, 0.75, 0.76, 0.80, 0.86, 0.92, 0.97, 1.00, 1.00, 1.00,
                                   0.99, 1.00, 1.00, 0.99, 0.97, 0.94, 0.91, 0.88, 0.86, 0.84, 0.82, 0.80)),
    "water": Kind(True, 90.0, (0.55, 0.50, 0.48, 0.48, 0.55, 0.75, 0.95, 1.00, 0.95, 0.85, 0.78, 0.75,
                               0.75, 0.72, 0.70, 0.72, 0.80, 0.90, 1.00, 0.98, 0.90, 0.80, 0.70, 0.60)),
    "telecom": Kind(True, 80.0, (0.88, 0.86, 0.85, 0.85, 0.85, 0.86, 0.88, 0.92, 0.96, 0.98, 1.00, 1.00,
                                 1.00, 1.00, 1.00, 0.99, 0.98, 0.97, 0.97, 0.97, 0.96, 0.95, 0.92, 0.90)),
    "shelter": Kind(True, 70.0, (0.95, 0.92, 0.90, 0.90, 0.90, 0.88, 0.80, 0.70, 0.65, 0.62, 0.60, 0.60,
                                 0.62, 0.62, 0.62, 0.65, 0.70, 0.80, 0.90, 1.00, 1.00, 1.00, 0.98, 0.97)),
    "residential": Kind(False, 2.0, (0.50, 0.45, 0.42, 0.40, 0.40, 0.45, 0.58, 0.70, 0.68, 0.62, 0.60, 0.60,
                                     0.62, 0.60, 0.60, 0.62, 0.70, 0.82, 0.95, 1.00, 1.00, 0.95, 0.80, 0.62)),
    "commercial": Kind(False, 1.0, (0.30, 0.28, 0.27, 0.27, 0.28, 0.30, 0.40, 0.60, 0.85, 0.95, 1.00, 1.00,
                                    0.98, 1.00, 1.00, 0.98, 0.95, 0.85, 0.70, 0.55, 0.45, 0.40, 0.35, 0.32)),
}


@dataclass(frozen=True)
class FeederSpec:
    """One feeder: its substation, its load parts (kind, peak kW) and its UFLS stage (0: exempt)."""
    name: str
    substation: str
    parts: Tuple[Tuple[str, float], ...]
    ufls_stage: int = 0

    def __post_init__(self):
        if not self.parts or any(k not in KINDS or p <= 0 for k, p in self.parts):
            raise ValueError(f"feeder {self.name}: parts must be known kinds with positive peaks")

    @property
    def critical(self) -> bool:
        return any(KINDS[k].critical for k, _ in self.parts)

    @property
    def weight(self) -> float:
        return max(KINDS[k].weight for k, _ in self.parts)

    def to_dict(self) -> dict:
        return {"name": self.name, "substation": self.substation, "parts": [list(p) for p in self.parts],
                "ufls_stage": self.ufls_stage, "critical": self.critical, "weight": self.weight}


def default_island() -> List[FeederSpec]:
    """4 substations x 3 feeders: one critical feeder each (mixed with houses), the rest
    residential or commercial. Critical feeders are exempt from UFLS (as hospitals are
    from rotating outages); the others trip in four stages."""
    def f(name, parts, stage=0):
        return FeederSpec(name, name.split("-")[0], tuple(parts), stage)
    return [
        f("S1-F1", [("hospital", 1200.0), ("residential", 1300.0)]),
        f("S1-F2", [("residential", 4300.0)], 1),
        f("S1-F3", [("commercial", 3000.0), ("residential", 1500.0)], 3),
        f("S2-F1", [("water", 900.0), ("residential", 1100.0)]),
        f("S2-F2", [("residential", 4600.0)], 1),
        f("S2-F3", [("commercial", 2600.0), ("residential", 1800.0)], 3),
        f("S3-F1", [("telecom", 400.0), ("residential", 1400.0)]),
        f("S3-F2", [("residential", 4200.0)], 4),
        f("S3-F3", [("commercial", 3200.0), ("residential", 1200.0)], 2),
        f("S4-F1", [("shelter", 700.0), ("residential", 1300.0)]),
        f("S4-F2", [("residential", 4400.0)], 4),
        f("S4-F3", [("commercial", 2800.0), ("residential", 1400.0)], 2),
    ]


def by_substation(specs: Iterable[FeederSpec]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for s in specs:
        out.setdefault(s.substation, []).append(s.name)
    return {k: sorted(v) for k, v in sorted(out.items())}


def shape_at(kind: str, hour: float) -> float:
    sh = KINDS[kind].shape
    h = hour % 24.0
    i = int(math.floor(h))
    frac = h - i
    return sh[i] * (1.0 - frac) + sh[(i + 1) % 24] * frac


class Loads:
    """Demand of every feeder over a run: forecast() without noise, demand() with it."""

    def __init__(self, cfg, specs: Sequence[FeederSpec], run: int = 0, scale: float = 1.0):
        self.cfg, self.run, self.scale = cfg, run, scale
        self.specs = {s.name: s for s in specs}
        n_min = int(math.ceil(cfg.horizon_s / 60.0)) + 120
        rho = 0.95                                   # minute-to-minute correlation of the noise
        self._noise: Dict[str, List[float]] = {}
        for name in sorted(self.specs):
            rng = random.Random(f"load-{cfg.seed}-{run}-{name}")
            sd = cfg.load_noise * math.sqrt(1.0 - rho * rho)
            e = rng.gauss(0.0, cfg.load_noise) if cfg.load_noise > 0 else 0.0
            xs: List[float] = []
            for _ in range(n_min + 1):
                xs.append(e)
                e = rho * e + (rng.gauss(0.0, sd) if cfg.load_noise > 0 else 0.0)
            self._noise[name] = xs

    def forecast(self, feeder: str, t: float) -> float:
        """Expected demand in kW at time t (the public forecast)."""
        hour = self.cfg.start_hour + t / HOUR
        return self.scale * sum(p * shape_at(k, hour) for k, p in self.specs[feeder].parts)

    def demand(self, feeder: str, t: float) -> float:
        """Actual demand in kW at time t, before any cold-load pickup."""
        xs = self._noise[feeder]
        m = max(0.0, t / 60.0)
        i = min(int(m), len(xs) - 2)
        e = xs[i] + (xs[i + 1] - xs[i]) * (m - i)
        return max(0.0, self.forecast(feeder, t) * (1.0 + e))

    def slot_forecast(self, feeder: str, t0: float) -> float:
        """Planning demand for the dispatch slot starting at t0: the larger end of the slot
        (the shapes are linear within an hour, so the maximum is at an end)."""
        return max(self.forecast(feeder, t0), self.forecast(feeder, t0 + self.cfg.dispatch_period_s))

    def peak_kw(self, step_s: float = 300.0) -> float:
        """The island's coincident peak demand over one day, from the forecast."""
        return max(sum(self.forecast(f, k * step_s) for f in self.specs) for k in range(int(86400 / step_s)))

    def critical_kw(self, t: float) -> float:
        return sum(self.forecast(f, t) for f, s in self.specs.items() if s.critical)


def island_meta(specs: Sequence[FeederSpec]) -> List[dict]:
    """The feeder table as it goes into a run's log metadata."""
    return [s.to_dict() for s in specs]


def specs_from_meta(rows: Iterable[Mapping]) -> List[FeederSpec]:
    return [FeederSpec(r["name"], r["substation"], tuple((k, float(p)) for k, p in r["parts"]), int(r["ufls_stage"]))
            for r in rows]
