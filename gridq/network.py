"""Links between nodes: delay, loss, outages and partitions.

Generalised from evsim/network.py (BlokcChin@796b43c), where only the
resident's access link could lose messages. Here every node (validator,
dispatch centre, substation) has a link, and every message from src to dst goes
through the same model:

  delay      net_delay_s + Uniform(0, net_jitter_s), drawn per message.
  iid        each send attempt is lost independently with link_loss_p.
  outage     (evsim's B1) each node's link is an on/off process, a
             Gilbert-Elliott channel: outages last Exp(mean =
             link_outage_mean_s) and up periods Exp(mean = outage_mean x
             (1 - p) / p), so a node is down a long-run share p of the time. A
             message is lost while either end is down, so a resend a second
             later fails too, which independent loss misses. Wartime jamming
             looks like this. Only nodes in `outage_nodes` (default: all) have
             outages.
  partition  during [start, end) only nodes in the same group can talk; a node
             in no group is cut off from everyone. Messages sent across groups
             are dropped, not queued (the protocol re-sends in later rounds).

Loss and partitions are decided at send time: a message sent just before a
partition starts is still delivered. A node's message to itself is local and
arrives at once.

Draws are keyed instead of coming from one shared stream (common random
numbers): loss and delay by (seed, run, msg_id, src, dst, attempt), outage
timelines by (seed, run, node). Two runs that differ only in the fault or the
architecture therefore see the same network wherever they send the same
messages, and an extra message in one run does not shift the draws of the other.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Partition:
    start: float
    end: float
    groups: Tuple[FrozenSet[str], ...]

    def active(self, t: float) -> bool:
        return self.start <= t < self.end

    def group_of(self, node: str) -> Optional[int]:
        for i, g in enumerate(self.groups):
            if node in g:
                return i
        return None

    def separates(self, a: str, b: str, t: float) -> bool:
        if a == b or not self.active(t):
            return False
        ga, gb = self.group_of(a), self.group_of(b)
        return ga is None or gb is None or ga != gb


class Network:
    def __init__(self, cfg, run: int = 0, outage_nodes: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self.run = run
        self.outage_nodes: Optional[FrozenSet[str]] = None if outage_nodes is None else frozenset(outage_nodes)
        self.partitions: List[Partition] = []
        self._down: Dict[str, Tuple[List[float], List[float]]] = {}

    # ---------- partitions ----------
    def add_partition(self, start: float, end: float, groups: Iterable[Iterable[str]]) -> Partition:
        p = Partition(start, end, tuple(frozenset(g) for g in groups))
        self.partitions.append(p)
        return p

    def partitioned(self, a: str, b: str, t: float) -> bool:
        return any(p.separates(a, b, t) for p in self.partitions)

    # ---------- outage model ----------
    def mean_up_s(self) -> float:
        p = self.cfg.link_loss_p
        return math.inf if p <= 0 else self.cfg.link_outage_mean_s * (1.0 - p) / p

    def outages(self, node: str) -> Tuple[List[float], List[float]]:
        """Sorted (starts, ends) of this node's outages over the run (plus an hour)."""
        if node not in self._down:
            p, md = self.cfg.link_loss_p, self.cfg.link_outage_mean_s
            starts: List[float] = []
            ends: List[float] = []
            if p >= 1.0:
                starts, ends = [-math.inf], [math.inf]
            elif p > 0.0:
                rng = random.Random(f"out-{self.cfg.seed}-{self.run}-{node}")
                mu = self.mean_up_s()
                end_t = self.cfg.horizon_s + 3600.0
                t = 0.0
                if rng.random() < p:                      # stationary start
                    d = rng.expovariate(1.0 / md)
                    starts.append(-math.inf)
                    ends.append(d)
                    t = d
                while t < end_t:
                    t += rng.expovariate(1.0 / mu)
                    d = rng.expovariate(1.0 / md)
                    starts.append(t)
                    ends.append(t + d)
                    t += d
            self._down[node] = (starts, ends)
        return self._down[node]

    def is_down(self, node: str, t: float) -> bool:
        if self.cfg.link_loss_model != "outage" or self.cfg.link_loss_p <= 0.0:
            return False
        if self.outage_nodes is not None and node not in self.outage_nodes:
            return False
        starts, ends = self.outages(node)
        i = bisect.bisect_right(starts, t) - 1
        return i >= 0 and t < ends[i]

    # ---------- per message ----------
    def _lost(self, msg_id: str, src: str, dst: str, attempt: int, t: float) -> bool:
        if self.partitioned(src, dst, t):
            return True
        p = self.cfg.link_loss_p
        if p <= 0.0:
            return False
        if self.cfg.link_loss_model == "outage":
            return self.is_down(src, t) or self.is_down(dst, t)
        r = random.Random(f"loss-{self.cfg.seed}-{self.run}-{msg_id}-{src}-{dst}-{attempt}").random()
        return r < p

    def delay(self, msg_id: str, src: str, dst: str, attempt: int = 0) -> float:
        j = self.cfg.net_jitter_s
        if j <= 0.0:
            return self.cfg.net_delay_s
        u = random.Random(f"dly-{self.cfg.seed}-{self.run}-{msg_id}-{src}-{dst}-{attempt}").random()
        return self.cfg.net_delay_s + u * j

    def deliver(self, msg_id: str, src: str, dst: str, t: float) -> Optional[float]:
        """Arrival time of the first attempt that gets through, or None if every attempt is lost."""
        if src == dst:
            return t
        for a in range(self.cfg.link_retries + 1):
            ta = t + a * self.cfg.link_retry_gap_s
            if not self._lost(msg_id, src, dst, a, ta):
                return ta + self.delay(msg_id, src, dst, a)
        return None

    def effective_loss(self, both_ends: bool = True) -> float:
        """P(a message is lost on every attempt), partitions aside.

        iid: p ** attempts. outage: the chance that at every attempt one of the
        two ends is down (both_ends, both ends have outages) or that one given
        end is down (both_ends=False, evsim's single access link)."""
        times = [a * self.cfg.link_retry_gap_s for a in range(self.cfg.link_retries + 1)]
        if both_ends and self.cfg.link_loss_model == "outage":
            return either_down_prob(self.cfg, times)
        return all_down_prob(self.cfg, times)


def _switch_rate(cfg) -> float:
    """k = 1 / mean outage + 1 / mean up time of the on/off chain."""
    p, md = cfg.link_loss_p, cfg.link_outage_mean_s
    return 1.0 / md + 1.0 / (md * (1.0 - p) / p)


def _p_down_down(cfg, gap: float) -> float:
    p = cfg.link_loss_p
    return p + (1.0 - p) * math.exp(-_switch_rate(cfg) * gap)


def all_down_prob(cfg, times: Sequence[float]) -> float:
    """P(one node's link is down at every one of these instants) (evsim's formula).

    iid: each instant is an independent attempt, p ** len(times). outage: a
    two-state Markov chain in stationarity, pi_down x prod P(down -> down over
    each gap)."""
    p = cfg.link_loss_p
    if not times:
        return 1.0
    if p <= 0.0:
        return 0.0
    if cfg.link_loss_model != "outage" or p >= 1.0:
        return p ** len(times)
    ts = sorted(times)
    out = p
    for a, b in zip(ts, ts[1:]):
        out *= _p_down_down(cfg, b - a)
    return out


def either_down_prob(cfg, times: Sequence[float]) -> float:
    """P(at every one of these instants, at least one of two independent nodes is down).

    Outage model: the pair of on/off chains is a four-state Markov chain; this
    walks it in stationarity, keeping only the states where a message is lost.
    iid: one draw per attempt, p ** len(times)."""
    p = cfg.link_loss_p
    if not times:
        return 1.0
    if p <= 0.0:
        return 0.0
    if cfg.link_loss_model != "outage" or p >= 1.0:
        return p ** len(times)
    pi = {0: 1.0 - p, 1: p}                         # 0 up, 1 down
    lost = [(x, y) for x in (0, 1) for y in (0, 1) if x or y]
    v = {s: pi[s[0]] * pi[s[1]] for s in lost}
    ts = sorted(times)
    for a, b in zip(ts, ts[1:]):
        dd = _p_down_down(cfg, b - a)
        ud = p * (1.0 - math.exp(-_switch_rate(cfg) * (b - a)))
        step = {(1, 1): dd, (1, 0): 1.0 - dd, (0, 1): ud, (0, 0): 1.0 - ud}   # (from, to) for one node
        v = {s2: sum(v[s1] * step[(s1[0], s2[0])] * step[(s1[1], s2[1])] for s1 in lost) for s2 in lost}
    return sum(v.values())
