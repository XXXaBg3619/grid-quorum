"""Message-level BFT consensus for architectures C and D (to be written next).

evsim's M2 abstracted Tendermint: exactly 4 validators, votes only approximated
by a probability, no per-node state. That cannot show an equivocating
validator, more than f validators offline, or a partition. This package
replaces it with a per-node state machine:

  state      height, round, step (propose / prevote / precommit), locked value
             and round, valid value and round
  proposer   validators[(height + round) % n]   (rotation kept from evsim's M2)
  quorum     cfg.quorum (Q = 5 of 7)
  timeouts   cfg.round_timeout(r), growing every round (partial synchrony)
  commit     Q precommits for one block; their signatures form the commit
             proof that substations check (crypto.py)
  safety     an honest node never precommits two blocks in one round and
             respects its lock; every run checks that no two different blocks
             commit at the same height
  messages   (n - 1) + 2n(n - 1) per round, the count evsim's M2 used
  check      with iid loss and no faults, the share of rounds that commit
             must match theory.round_success_prob(n, Q, p)

Byzantine behaviour comes only from the faults.py hooks.
"""
