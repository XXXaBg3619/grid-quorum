"""Closed forms and reference values the simulator is checked against.

round_success_prob() is ported from evsim/mechanisms/m2.py (BlokcChin@796b43c):
the chance that one Tendermint-style round commits when every consensus message
is lost independently with probability p and not re-sent within the round.
Here it is the reference answer the message-level consensus (consensus/) has to
reproduce under iid loss (experiments/check_consensus.py). Two changes from
evsim: the quorum q is passed in instead of 2f + 1, so the same code covers
Q = floor(2n/3) + 1 for any n; and only a validator that got the proposal may
precommit (see round_ok).

commit_latency_bound() is new: the latest first commit of a height with no
message loss, when the first rounds' proposers are silent.

Counting nodes (experiments 3 and 6): binom_tail, poisson_binom_tail;
quorum_up_prob, the chance that at least Q validators are up when each is down
independently; and the common-cause model of design doc question 19.
compromise_prob_events: independent common-cause events (an organisation's
shared accounts, staff and network; one software implementation) each take
all of their nodes at once, otherwise each node falls on its own;
compromise_prob is the case of one event over every node, and sample_compromised
draws one outcome of the same model (the Monte Carlo check in experiment 6).
C (one operator) has a high common cause and low node risks; D (one node per
organisation) keeps only the shared software as common cause, but its weaker
organisations have higher node risks. break_even_weak_risk gives the boundary
where the two are equally likely to lose k nodes. The probabilities are
assumptions: the model shows trends, not real odds.
"""
from __future__ import annotations

import functools
import math
import random
from typing import Callable, Iterable, Sequence, Set, Tuple


def round_outcome(n: int, q: int, proposer: int, got: Callable[[str, int, int], bool]) -> bool:
    """Does one round commit, given which messages arrive?

    got(kind, i, j): did validator i's message of this kind ("proposal",
    "prevote", "precommit") reach j. A validator's message to itself always
    arrives. A validator that got the proposal prevotes for it; one that got
    the proposal and counts q prevotes (its own included) precommits; any
    validator that counts q precommits commits, since the block travels with
    the precommits (standing in for CometBFT's block-part gossip). The round
    decides if at least one validator commits; the others catch up.

    evsim's version let a validator without the proposal precommit as well.
    Tendermint and CometBFT precommit nil on a polka for a block they do not
    have, so here only prevoters can precommit."""
    prevoters = [i for i in range(n) if i == proposer or got("proposal", proposer, i)]
    precommitters = [j for j in prevoters
                     if sum(1 for i in prevoters if i == j or got("prevote", i, j)) >= q]
    if len(precommitters) < q:
        return False
    return any(sum(1 for i in precommitters if i == j or got("precommit", i, j)) >= q for j in range(n))


def round_ok(n: int, q: int, p: float, proposer: int, rng: random.Random) -> bool:
    """One round in which every message is lost independently with probability p."""
    return round_outcome(n, q, proposer, lambda kind, i, j: rng.random() >= p)


@functools.lru_cache(maxsize=None)
def round_success_prob(n: int, q: int, p: float, trials: int = 20000) -> float:
    """Monte Carlo estimate of the chance that one round commits (fixed seed, so deterministic).

    Cached: the estimate depends only on (n, q, p, trials)."""
    rng = random.Random(f"round-ok-{n}-{q}-{p}")
    ok = sum(round_ok(n, q, p, 0, rng) for _ in range(trials))
    return max(ok / trials, 1.0 / trials)


def commit_latency_bound(cfg, silent_rounds: int = 0) -> float:
    """Latest first commit, counted from the start of a height, when every
    validator starts the height at the same time, no message is lost, at most
    f validators are down, and the proposers of the first `silent_rounds`
    rounds are down or silent.

    A round whose proposer is up commits within three hops (proposal, prevote,
    precommit), a hop taking at most cfg.max_hop_s(). A silent round r ends
    exactly round_timeout(r) + 2 vote_timeout(r) after it starts, for every
    node (consensus/tendermint.py, timers), so the validators also start the
    next round together."""
    silent = sum(cfg.round_timeout(r) + 2 * cfg.vote_timeout(r) for r in range(silent_rounds))
    return silent + 3 * cfg.max_hop_s()


# ---------------------------------------------------------------- counting nodes

def binom_tail(n: int, p: float, k: int) -> float:
    """P(Bin(n, p) >= k)."""
    return min(1.0, sum(math.comb(n, j) * p ** j * (1.0 - p) ** (n - j) for j in range(max(k, 0), n + 1)))


def poisson_binom_tail(ps: Sequence[float], k: int) -> float:
    """P(at least k of independent events with probabilities ps happen)."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for j, w in enumerate(dist):
            nxt[j] += w * (1.0 - p)
            nxt[j + 1] += w * p
        dist = nxt
    return min(1.0, sum(dist[max(k, 0):]))


def quorum_up_prob(n: int, q: int, p_down: float) -> float:
    """P(at least q of n validators are up) when each is down independently with p_down."""
    return binom_tail(n, 1.0 - p_down, q)


def compromise_prob(k: int, p_common: float, p_nodes: Sequence[float]) -> float:
    """P(at least k validators compromised in one period) under the common-cause model.

    With probability p_common one event takes every node at once (C: shared
    accounts, staff and software; D: a zero-day in the one software every node
    runs); otherwise node j falls on its own with p_nodes[j], independently."""
    if k <= 0:
        return 1.0
    if k > len(p_nodes):
        return 0.0
    return p_common + (1.0 - p_common) * poisson_binom_tail(p_nodes, k)


def compromise_prob_events(k: int, events: Sequence[Tuple[float, Iterable[int]]], p_nodes: Sequence[float]) -> float:
    """P(at least k of the nodes compromised in one period) with several common-cause events.

    events: (probability, node indices) pairs, independent of each other and of
    the nodes; an event takes all of its nodes at once (an organisation's shared
    accounts, staff and network; one software implementation's zero-day).
    Otherwise node j falls on its own with p_nodes[j]. Exact: a sum over the
    2^len(events) combinations of events."""
    n = len(p_nodes)
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    evs = [(p, frozenset(nodes)) for p, nodes in events]
    total = 0.0
    for mask in range(1 << len(evs)):
        w, taken = 1.0, set()
        for i, (p, nodes) in enumerate(evs):
            if mask >> i & 1:
                w, taken = w * p, taken | nodes
            else:
                w *= 1.0 - p
        if w > 0.0:
            total += w * poisson_binom_tail([p_nodes[j] for j in range(n) if j not in taken], k - len(taken))
    return min(1.0, total)


def sample_compromised(events: Sequence[Tuple[float, Iterable[int]]], p_nodes: Sequence[float],
                       rng: random.Random) -> Set[int]:
    """One draw of compromise_prob_events' model: the indices of the nodes that fall."""
    out: Set[int] = set()
    for p, nodes in events:
        if rng.random() < p:
            out |= set(nodes)
    out |= {j for j, p in enumerate(p_nodes) if rng.random() < p}
    return out


def break_even_weak_risk(k: int, p_c: float, p_strong: float, p_shared: float, n: int, n_weak: int,
                         tol: float = 1e-9) -> float:
    """The weak organisations' node risk at which D is as likely as C to lose k nodes.

    C: n nodes of one operator, common cause p_c, node risk p_strong each.
    D: n organisations, common cause p_shared (the software), n - n_weak strong
    ones with p_strong and n_weak weak ones with the returned risk. Below it D
    is the safer design. p_strong when the two are equal with weak = strong
    (p_c = p_shared), 0.0 if D is not safer even then, 1.0 if D stays safer at
    any weak risk."""
    target = compromise_prob(k, p_c, [p_strong] * n)
    d = lambda w: compromise_prob(k, p_shared, [p_strong] * (n - n_weak) + [w] * n_weak)  # noqa: E731
    lo, hi = p_strong, 1.0
    gap = d(lo) - target
    if gap > 1e-12:
        return 0.0
    if gap >= -1e-12:
        return p_strong
    if d(hi) < target:
        return 1.0
    while hi - lo > tol:
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if d(mid) < target else (lo, mid)
    return (lo + hi) / 2.0
