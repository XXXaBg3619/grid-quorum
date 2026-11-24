"""B: central dispatch plus a local policy enforcer at each substation.

Like A, but each substation's enforcer refuses commands that break a rule it
can check on its own (opening a hospital feeder, shedding too much in one
step), on top of the existing under-frequency load shedding. When the centre is
lost, substations run a pre-signed rotation table (the static fallback is part
of B's definition).

Stops: obviously violating single commands. Misses: commands that are each
valid but harmful together (experiment 5), and after losing the centre it can
only follow the fixed table instead of the current state.

To be written next: policy.check_local at the substation, the rotation table,
FALLBACK_SCHEDULE events.
"""
from .base import Architecture


class ArchB(Architecture):
    name = "B"
    decision_path = "dispatch centre decides; substation enforcer checks each command locally; static rotation table on failure"

    def on_dispatch(self, k, t):
        raise NotImplementedError("architecture B is not written yet (docs/DESIGN.md)")
