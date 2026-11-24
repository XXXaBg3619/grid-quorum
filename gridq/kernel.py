"""Discrete-event kernel (extracted from evsim/engine.py, BlokcChin@796b43c).

A clock and a priority queue, nothing else. Events at the same time run in
priority order and then in the order they were scheduled, so a run is fully
deterministic. Everything grid-specific (dispatch ticks, consensus messages,
substations, the power balance) schedules callbacks here; see sim.py.
"""
from __future__ import annotations

import heapq
import itertools
from typing import Callable, Optional

# Same-time ordering, lower runs first: physics changes the state, messages are
# handled against it, dispatch decisions see both, measurements read last.
PRIO_PHYSICS, PRIO_MESSAGE, PRIO_DISPATCH, PRIO_MEASURE = 0, 1, 2, 3

EPS = 1e-9


class Kernel:
    def __init__(self, horizon_s: float):
        self.horizon_s = horizon_s
        self.now = 0.0
        self.processed = 0
        self._q: list = []
        self._ctr = itertools.count()

    def schedule(self, t: float, prio: int, fn: Callable, *args) -> None:
        """Run fn(*args) at time t (never in the past)."""
        if t < self.now - EPS:
            raise ValueError(f"cannot schedule in the past: t={t} < now={self.now}")
        heapq.heappush(self._q, (t, prio, next(self._ctr), fn, args))

    def after(self, delay: float, prio: int, fn: Callable, *args) -> None:
        self.schedule(self.now + delay, prio, fn, *args)

    def pending(self) -> int:
        return len(self._q)

    def run(self, until: Optional[float] = None) -> None:
        """Process events up to `until` (default and maximum: the horizon).

        Later events stay queued, so run() can be called again to continue."""
        end = self.horizon_s if until is None else min(until, self.horizon_s)
        while self._q and self._q[0][0] <= end + EPS:
            t, _, _, fn, args = heapq.heappop(self._q)
            self.now = t
            fn(*args)
            self.processed += 1
        self.now = max(self.now, end)
