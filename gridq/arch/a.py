"""A: central dispatch plus a backup centre (the status quo; evsim's M0).

One dispatch centre computes the policy and sends signed commands; a backup
centre takes over when the centre is bombed or cut off (central.CentralPath).
Survives losing the centre. Fails when the centre is hacked: the backup
executes the same wrong commands, and substations only check the centre's
signature. When both centres are lost, substations hold their state; only
UFLS acts (question 26).
"""
from ..central import CentralPath
from .base import Architecture


class ArchA(Architecture):
    name = "A"
    decision_path = "dispatch centre decides; backup takes over on failure; substation checks one signature"

    def central(self, **kw) -> CentralPath:
        sim = self.sim
        verifier_kw = {**sim.verifier_kw(), **kw.pop("verifier_kw", {})}
        return CentralPath(sim.cfg, sim.kernel, sim.net, specs=sim.specs, measure=sim.measure, log=sim.log,
                           fault=sim.fault, checks=sim.checks, clock_offset_s=sim.clock_offset_s,
                           verifier_kw=verifier_kw, **kw)

    def build(self):
        return self.central()

    @property
    def orgs(self):
        return {c: "taipower" for c in self.link.centres}
