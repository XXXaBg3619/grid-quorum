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

To add next (docs/DESIGN.md): P(more than f validators compromised) under the
common-cause model (node risk = shared p_c + independent p_i) for C vs D.
"""
from __future__ import annotations

import functools
import random
from typing import Callable


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
