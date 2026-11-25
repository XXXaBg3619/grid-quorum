"""Live mode: the simulator's consensus and verifier on a real network (the Docker demo, new in gridq).

The simulator runs every node in one process on a simulated clock. Live mode
runs one node per process on the wall clock, sends UDP messages and signs with
Ed25519, so the decision path of architectures C and D can be shown on real
containers (demo/):

  keys        Ed25519 key ring with crypto.KeyRing's interface; a process
              holds only its own private key and everyone's public keys
  runtime     wall-clock kernel with kernel.Kernel's interface, UDP
              transport, the status endpoint, the console log, the main loop
  validator   one validator: consensus.Committee driven for the local node,
              lasthop.RotatingDispatch, state reports, sync answers and a
              double-sign guard
  substation  one substation: substation.Substation and verifier.Verifier,
              unchanged, behind a live link
  attacker    someone on the last hop: replay, tamper, forge with stolen
              keys, wrong chain
  monitor     polls every node: console table, dashboard page, fault switch

The consensus, dispatch and verifier code is the simulator's, not a copy.
Live mode needs the `cryptography` package; the simulator does not.
"""
