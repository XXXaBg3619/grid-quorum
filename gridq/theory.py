"""Closed forms and reference values the simulator is checked against.

Ported from evsim/mechanisms/m2.py (BlokcChin@796b43c): round_success_prob(),
the chance that one Tendermint-style round commits when every consensus
message is lost independently with probability p and not re-sent within the
round. evsim used it to fast-forward idle heights; here it is the reference
answer the message-level consensus (consensus/) has to reproduce under iid
loss. The only change: the quorum q is passed in instead of 2f + 1, so the
same code covers Q = floor(2n/3) + 1 for any n.

To add next (docs/DESIGN.md): commit-latency bounds for honest and
silent-proposer rounds, and P(more than f validators compromised) under the
common-cause model (node risk = shared p_c + independent p_i) for C vs D.
"""
from __future__ import annotations

import functools
import random


def round_ok(n: int, q: int, p: float, proposer: int, rng: random.Random) -> bool:
    """One round with lossy messages, no resends within the round.

    A validator prevotes the block if it got the proposal, precommits if it
    counts q prevotes (its own included) and commits on q precommits. The round
    decides if at least one validator commits; the others catch up by gossip."""
    got = lambda: rng.random() >= p
    prevoters = [i for i in range(n) if i == proposer or got()]
    precommitters = [j for j in range(n)
                     if sum(1 for i in prevoters if i == j or got()) >= q]
    if len(precommitters) < q:
        return False
    return any(sum(1 for i in precommitters if i == j or got()) >= q for j in range(n))


@functools.lru_cache(maxsize=None)
def round_success_prob(n: int, q: int, p: float, trials: int = 20000) -> float:
    """Monte Carlo estimate of the chance that one round commits (fixed seed, so deterministic).

    Cached: the estimate depends only on (n, q, p, trials)."""
    rng = random.Random(f"round-ok-{n}-{q}-{p}")
    ok = sum(round_ok(n, q, p, 0, rng) for _ in range(trials))
    return max(ok / trials, 1.0 / trials)
