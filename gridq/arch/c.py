"""C: Taipower-internal BFT committee (similar to Spire).

Seven validators at Taipower sites run the same consensus as D (consensus/):
each block carries the substations' signed state reports and the slot's
measurements, and every validator recomputes the policy's commands from them
(lasthop.PolicyDispatch), so a proposal that is not exactly what the rules
give for the agreed state is refused (the global check). Allocation stays
dynamic while up to f validators are lost. Weakness: one administrative
domain, so shared accounts, staff and software let a single intrusion take
many nodes at once (common-cause failure, experiment 6), and all input data
comes from Taipower.
"""
from ..lasthop import LastHop, PolicyDispatch
from ..loads import by_substation
from .base import Architecture


class ArchC(Architecture):
    name = "C"
    decision_path = "7 Taipower validators decide by BFT (Q of n); substation verifies the commit proof"
    ORGS = ("taipower",)

    def build(self):
        sim = self.sim
        return LastHop(sim.cfg, sim.kernel, sim.net, feeders=by_substation(sim.specs), log=sim.log, fault=sim.fault,
                       checks=sim.checks, clock_offset_s=sim.clock_offset_s,
                       dispatch=lambda sub_keys, reports_of: PolicyDispatch(sim.cfg, sim.specs, sub_keys, sim.measure,
                                                                            reports_of),
                       verifier_kw=sim.verifier_kw())

    @property
    def orgs(self):
        names = self.link.com.names
        return {v: self.ORGS[i % len(self.ORGS)] for i, v in enumerate(names)}
