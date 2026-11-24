"""B: central dispatch plus a local policy enforcer at each substation.

Like A, but each substation's enforcer (policy.LocalEnforcer) refuses commands
that break a rule it can check on its own: opening a critical feeder, more than
cfg.local_max_switch switchings in one dispatch period. UFLS stays as it is.
When no centre has been heard for cfg.fallback_after_s, substations run a
pre-signed rotation table (policy.static_table, planned in advance for
cfg.plan_supply_share of the peak demand) until a centre is heard again; the
static fallback is part of B's definition.

Stops: obviously violating single commands. Misses: commands that are each
valid but harmful together (experiment 5), and after losing the centre it can
only follow the fixed table instead of the current state: a table planned for
40 % of the peak overloads an island left with 25 % (question 22).
"""
from ..loads import by_substation
from ..policy import LocalEnforcer, static_table
from .a import ArchA


class ArchB(ArchA):
    name = "B"
    decision_path = ("dispatch centre decides; substation enforcer checks each command locally; "
                     "static rotation table on failure")

    def build(self):
        sim, cfg = self.sim, self.cfg
        self.enforcer = LocalEnforcer(cfg, sim.specs)
        self.plan_kw = cfg.plan_supply_share * sim.peak_kw
        table = static_table(cfg, sim.specs, sim.loads, self.plan_kw, cfg.n_dispatches)
        tables = {s: [{f: row[f] for f in fs} for row in table] for s, fs in by_substation(sim.specs).items()}
        return self.central(verifier_kw={"policy_fn": self.enforcer}, tables=tables)

    def finish(self) -> None:
        super().finish()
        self.sim.log.run_meta["arch"].update(plan_kw=round(self.plan_kw, 1),
                                             enforcer_refused=dict(sorted(self.enforcer.refused.items())))
