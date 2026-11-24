"""Event log: hash chain, JSONL round trip, public / truth split (ported from evsim tests/test_core.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.events import GENESIS, EventKind, EventLog  # noqa: E402


def sample_log() -> EventLog:
    log = EventLog(run_meta={"run": 0, "arch": "D"})
    log.append(0.0, EventKind.SUPPLY_CHANGED, "sim", {"supply_kw": 1200.0})
    log.append(1.5, EventKind.PROPOSAL, "validator:0", {"height": 1, "round": 0, "block": "ab"})
    log.append(1.6, EventKind.FAULT_ACTION, "validator:3", {"fault": "equivocate"}, visibility="truth")
    log.append(2.0, EventKind.COMMAND_REJECTED, "substation:S1", {"reason": "stale_seq"})
    return log


class TestEventLog(unittest.TestCase):
    def test_chain_and_roundtrip(self):
        log = sample_log()
        self.assertTrue(log.verify_chain())
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "run.jsonl")
            log.write_jsonl(p)
            back = EventLog.read_jsonl(p)
        self.assertTrue(back.verify_chain())
        self.assertEqual(back.head, log.head)
        self.assertEqual(back.run_meta, log.run_meta)
        self.assertEqual([e.kind for e in back.events], [e.kind for e in log.events])

    def test_tampering_breaks_chain(self):
        log = sample_log()
        log.events[1].data["block"] = "cd"
        self.assertFalse(log.verify_chain())

    def test_public_and_truth_split(self):
        log = sample_log()
        self.assertEqual(len(list(log.public())), 3)
        self.assertEqual(len(list(log.of_kind(EventKind.FAULT_ACTION, visibility="truth"))), 1)
        self.assertEqual(list(log.of_kind(EventKind.FAULT_ACTION, visibility="public")), [])

    def test_bad_visibility_rejected(self):
        with self.assertRaises(ValueError):
            EventLog().append(0.0, EventKind.COMMIT, "validator:0", {}, visibility="secret")

    def test_empty_log(self):
        self.assertEqual(EventLog().head, GENESIS)
        self.assertTrue(EventLog().verify_chain())


if __name__ == "__main__":
    unittest.main()
