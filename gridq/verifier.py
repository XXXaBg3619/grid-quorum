"""Substation verifier: the light node at the last hop (to be written next).

A command is executed only if every check passes (design doc, second external
review); otherwise it is rejected with the first failing reason, which goes
into the COMMAND_REJECTED event:

  wrong_chain          chain_id is not this island's chain
  wrong_epoch          not the fixed validator set (epoch changes are future work)
  stale_seq            height / sequence not newer than the last command this
                       substation executed: a replay (experiment 4)
  unknown_feeder       not a feeder of this substation
  relative_target      the target must be an absolute state (on / off), never a step
  state_mismatch       the state-version precondition does not match the
                       substation's current state version
  insufficient_quorum  fewer than Q valid precommit signatures from distinct
                       validators over the block hash (consensus.verify_commit,
                       which also gives wrong_chain and wrong_epoch)
  bad_signature        A / B: the dispatch centre's signature does not verify
  header_mismatch      the block's header does not extend the chain this
                       substation trusts (chain.verify_chain with checkpoints)
  expired              past its TTL; a secondary check only
  policy_violation     B only: policy.check_local refused the command

Fail-safe: with no fresh valid command the substation holds its state and only
local protective shedding (overload, UFLS) may act. No automatic re-close and
no switch to a fallback schedule, except in B, whose static plan is its
definition.
"""

REJECT_REASONS = (
    "wrong_chain", "wrong_epoch", "stale_seq", "unknown_feeder", "relative_target",
    "state_mismatch", "insufficient_quorum", "bad_signature", "header_mismatch",
    "expired", "policy_violation",
)
