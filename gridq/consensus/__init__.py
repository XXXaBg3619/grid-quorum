"""Message-level BFT consensus for architectures C and D.

evsim's M2 abstracted Tendermint: exactly 4 validators, votes only approximated
by a probability, no per-node state. That cannot show an equivocating
validator, more than f validators offline, or a partition. This package
replaces it with one state machine per validator (tendermint.py):

  state      height, round, step (propose / prevote / precommit), locked value
             and round, valid value and round
  proposer   validators[(height + round) % n]   (rotation kept from evsim's M2)
  quorum     cfg.quorum (Q = 5 of 7); round skip on more than n/3 (3 of 7)
  timeouts   propose cfg.round_timeout(r), prevote and precommit
             cfg.vote_timeout(r), all growing every round (partial synchrony)
  commit     Q precommits for one block; their signatures are the commit proof
             that substations check (messages.verify_commit)
  safety     an honest node never precommits two blocks in one round and
             respects its lock; Committee.conflicting_heights() lists every
             height at which two different blocks were committed
  messages   (n - 1) + 2n(n - 1) per round, the count evsim's M2 used
  check      with iid loss and no faults, the share of heights that commit in
             round 0 matches theory.round_success_prob(n, Q, p)
             (experiments/check_consensus.py)

Byzantine behaviour comes only from the faults.py hooks.
"""
from .messages import (PRECOMMIT, PREVOTE, block_id, make_block, make_proof, proposal_digest,
                       verify_commit, vote_digest)
from .tendermint import PROPOSE, Committee, Validator, run_committee

__all__ = ["PREVOTE", "PRECOMMIT", "PROPOSE", "Committee", "Validator", "run_committee", "block_id",
           "make_block", "make_proof", "proposal_digest", "verify_commit", "vote_digest"]
