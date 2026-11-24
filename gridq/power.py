"""Simplified island power model (to be written next).

Only what the design doc keeps. This is a distributed-systems project: no power
flow and no protection transients; protective relays keep acting locally as
they do today.

  supply         G(t), the generation still available inside the island, from
                 scenarios (step drops when lines are cut, slow recovery)
  feeders        id, substation, load profile in kW over time (loads.py),
                 priority weight, critical flag, on/off state, time since it
                 was last energised
  cold load      a feeder re-energised after an outage draws
                 cfg.cold_load_multiplier x its normal load, decaying back over
                 cfg.cold_load_decay_s
  balance        served load against G(t): above G(t) x (1 + cfg.ufls_margin),
                 simplified under-frequency load shedding (UFLS) trips the
                 lowest-priority feeders; if that is not enough, the island
                 collapses
  state version  kept by each substation's verifier (verifier.py), +1 for
                 every executed command; local protective actions (UFLS trips)
                 must bump it too, so a command computed before a trip is
                 rejected

Metrics read from the log: critical-load hours, UFLS trips, island collapses.
"""
