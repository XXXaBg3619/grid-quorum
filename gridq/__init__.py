"""gridq: discrete-event simulator for wartime emergency power allocation.

An island of the grid keeps allocating its remaining supply by the
government's rules even when a dispatch centre is hacked or destroyed. The
simulator compares four architectures under one policy and one set of
scenarios:

  A  central dispatch plus a backup centre
  B  central dispatch plus a local policy enforcer at each substation
  C  Taipower-internal BFT (one administrative domain)
  D  multi-organisation BFT (7 validators, f = 2, Q = 5)

docs/DESIGN.md has the module map and what is still to be written. Modules
marked "ported" come from evsim (BlokcChin@796b43c).
"""
__version__ = "0.1.0"
