"""Simulator: one island, one architecture, one run (to be written next).

Planned shape, the same split as evsim's engine (BlokcChin@796b43c): the
simulator owns time and the log; everything architecture-specific lives in an
Architecture, everything adversarial in a Fault.

  Simulator(cfg, feeders, supply, arch_cls, fault, run=0)
    kernel   Kernel(cfg.horizon_s)            clock and queue           kernel.py
    log      EventLog(run_meta=...)           hash-chained event log    events.py
    net      Network(cfg, run)                delay, loss, partitions   network.py
    keys     KeyRing(nodes, cfg.seed)         simulated signatures      crypto.py
    island   IslandState                      feeders, G(t), version    power.py
    arch     arch_cls(self)                   A / B / C / D path        arch/
    fault    Fault                            hooks the arch calls      faults.py
    emit(kind, actor, data, visibility)       append to the log at kernel.now

  run():
    1. schedule G(t) changes and a dispatch tick every cfg.dispatch_period_s;
    2. at each tick the architecture computes target feeder states with
       policy.py and sends them through its own path (a signed central
       command, or a committee decision with a commit proof);
    3. substations verify (verifier.py), execute or hold, and log the outcome;
    4. power.py checks the balance and applies protective shedding or UFLS;
    5. metrics.py reads the finished log.
"""
