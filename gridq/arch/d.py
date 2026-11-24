"""D: multi-organisation BFT committee (the proposed design; evsim's M2).

Seven organisations (Taipower district office, city emergency centre,
hospital, water utility, ...) each run one validator: n = 7, f = 2, Q = 5.
The code path is C's (consensus, lasthop.PolicyDispatch, the substation
verifier); only the ownership of the validators differs, which the
common-cause model of experiment 6 uses. A substation executes only commands
with at least Q valid signatures and a newer sequence, and holds its state
when no fresh commit arrives. Survives up to f hacked plus unreachable nodes,
with one organisation's breach touching only one node. Costs: the most
expensive and complex option, the weakest organisation lowers overall
security, and shared input data can still be false.
"""
from .c import ArchC


class ArchD(ArchC):
    name = "D"
    decision_path = "7 validators from 7 organisations decide by BFT (Q = 5 of 7); substation verifies the commit proof"
    ORGS = ("taipower_district", "city_emergency", "hospital", "water_utility", "telecom", "fire_department",
            "central_government")
