"""C: Taipower-internal BFT committee (similar to Spire).

Seven validators at Taipower sites run the same consensus as D (consensus/),
check every proposal against the global island state (policy.check_global)
and keep allocating dynamically when one site is lost. Survives one site being
hacked or bombed, up to f. Weakness: one administrative domain, so shared
accounts, staff and software let a single intrusion take many nodes at once
(common-cause failure, experiment 6), and all input data comes from Taipower.

To be written next: shares the committee code (consensus.Committee) with D;
differs only in node ownership, which faults.py uses for the common-cause model.
"""
from .base import Architecture


class ArchC(Architecture):
    name = "C"
    decision_path = "7 Taipower validators decide by BFT (Q of n); substation verifies the commit proof"

    def on_dispatch(self, k, t):
        raise NotImplementedError("architecture C is not written yet (docs/DESIGN.md)")
