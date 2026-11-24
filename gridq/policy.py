"""Allocation policy: the government's rules as one pure function (to be written next).

Principles kept from evsim/allocation.py (BlokcChin@796b43c):
  * pure and deterministic: same inputs, same output, so every validator
    computes the same decision and anyone can recompute it from the log;
  * order-independent: inputs are sorted by objective fields (feeder id,
    priority) before any order-sensitive step;
  * one policy for all four architectures.

Planned rule (design doc, simplified power model):
  1. critical feeders first (hospital, water pumping, telecom, shelters); if
     they alone exceed G(t), rotate among them by priority weight;
  2. fill the remaining supply by priority weight, counting cold-load pickup
     (a feeder restored after an outage draws more for a while);
  3. keep a reserve margin and never plan above G(t).

Two checks the architectures differ on (experiment 5):
  check_local(cmd, feeder_state)   what B's substation enforcer can see: one
                                   command on its own (never open a hospital
                                   feeder, limit the size of one step)
  check_global(cmds, island)       what C and D validators see: the whole set of
                                   commands against G(t), which catches commands
                                   that are each valid but overload the island together
"""
