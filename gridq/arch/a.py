"""A: central dispatch plus a backup centre (the status quo; evsim's M0).

One dispatch centre computes the policy and sends signed commands; a backup
centre takes over when the centre is bombed or cut off. Survives losing the
centre. Fails when the centre is hacked: the backup executes the same wrong
commands, and substations only check the centre's signature.

To be written next: commands signed by the active centre, failover after
missed heartbeats, a hacked centre through faults.Fault.central_commands.
"""
from .base import Architecture


class ArchA(Architecture):
    name = "A"
    decision_path = "dispatch centre decides; backup takes over on failure; substation checks one signature"

    def on_dispatch(self, k, t):
        raise NotImplementedError("architecture A is not written yet (docs/DESIGN.md)")
