"""Substation verifier (light node): every reject reason, check order, check groups, A/B commands."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.chain import GENESIS  # noqa: E402
from gridq.config import GridConfig  # noqa: E402
from gridq.consensus import PRECOMMIT, PREVOTE, block_id, make_block, make_proof, vote_digest  # noqa: E402
from gridq.crypto import KeyRing  # noqa: E402
from gridq.events import EventKind  # noqa: E402
from gridq.verifier import ALL_CHECKS, REJECT_REASONS, Verifier, make_command, sign_command  # noqa: E402

CFG = GridConfig()
NAMES = [f"v{i}" for i in range(7)]
KEYS = KeyRing(NAMES, CFG.seed)
FEEDERS = {"S1-F1": "on", "S1-F2": "on"}


def cmd(feeder, target, seq, version, expires=10_000.0, sub="S1"):
    return make_command(sub, feeder, target, seq, version, expires)


def signed(blk, signers=NAMES[:5], cfg=CFG, kind=PRECOMMIT, keys=KEYS):
    bid = block_id(blk)
    return make_proof(blk["height"], 0, bid,
                      {v: keys.sign(v, vote_digest(cfg, kind, blk["height"], 0, bid)) for v in signers})


class Chain:
    """Blocks 1, 2, ... linked by hash, each with a commit proof from Q validators."""

    def __init__(self, cfg=CFG):
        self.cfg, self.prev, self.h = cfg, GENESIS, 0

    def add(self, *cmds):
        self.h += 1
        blk = make_block(self.cfg, self.h, self.prev, self.h - 1, "v0", 0, {"reports": [], "commands": list(cmds)})
        proof = signed(blk, cfg=self.cfg)
        self.prev = proof["block"]
        return blk, proof


def verifier(**kw):
    return Verifier(CFG, "S1", FEEDERS, keys=KEYS, **kw)


def reasons(v):
    return [e.data["reason"] for e in v.log.of_kind(EventKind.COMMAND_REJECTED)]


def count(v, kind):
    return sum(1 for _ in v.log.of_kind(kind))


class TestAccept(unittest.TestCase):
    def test_executes_commands_in_order(self):
        v, c = verifier(), Chain()
        b1, p1 = c.add(cmd("S1-F1", "off", 1, 0), cmd("S1-F2", "on", 2, 1), cmd("S2-F1", "off", 1, 0, sub="S2"))
        self.assertEqual(v.on_block(b1, p1, 0.1), "applied")
        self.assertEqual(v.feeders, {"S1-F1": "off", "S1-F2": "on"})
        self.assertEqual((v.version, v.last_seq, v.tip_height, v.tip_hash), (2, 2, 1, p1["block"]))
        self.assertEqual(count(v, EventKind.COMMAND_ACCEPTED), 2)      # S2's command is not for S1
        self.assertEqual(count(v, EventKind.FEEDER_SWITCHED), 1)       # S1-F2 was already on

    def test_copy_of_the_trusted_header_is_dropped_quietly(self):
        v, c = verifier(), Chain()
        b1, p1 = c.add(cmd("S1-F1", "off", 1, 0))
        v.on_block(b1, p1, 0.1)
        self.assertEqual(v.on_block(b1, dict(p1), 0.2), "duplicate")
        self.assertEqual(len(v.log.events), 2)                         # accepted + switched, nothing new

    def test_a_block_ahead_waits_for_the_gap(self):
        v, c = verifier(), Chain()
        b1, p1 = c.add(cmd("S1-F1", "off", 1, 0))
        b2, p2 = c.add(cmd("S1-F1", "on", 2, 1))
        self.assertEqual(v.on_block(b2, p2, 0.1), "gap")
        self.assertEqual((v.version, v.feeders["S1-F1"]), (0, "on"))
        self.assertEqual(v.on_block(b1, p1, 0.2), "applied")
        self.assertEqual(v.on_block(b2, p2, 0.3), "applied")
        self.assertEqual((v.version, v.tip_height, v.feeders["S1-F1"]), (2, 2, "on"))


class TestReplay(unittest.TestCase):
    """Question 25: "cut feeder 1" sent again after power came back."""

    def setUp(self):
        c = Chain()
        self.cut = c.add(cmd("S1-F1", "off", 1, 0, expires=900.0))
        self.restore = c.add(cmd("S1-F1", "on", 2, 1, expires=1200.0))

    def run_replay(self, t=400.0, **kw):
        v = verifier(**kw)
        v.on_block(*self.cut, 1.0)
        v.on_block(*self.restore, 301.0)
        status = v.on_block(*self.cut, t)
        return v, status

    def test_full_verifier_rejects_the_old_block(self):
        v, status = self.run_replay()
        self.assertEqual((status, reasons(v), v.feeders["S1-F1"]), ("rejected", ["stale_seq"], "on"))

    def test_order_or_version_alone_is_enough(self):
        for checks, reason in (({"order"}, "stale_seq"), ({"version"}, "state_mismatch")):
            v, _ = self.run_replay(checks=checks)
            self.assertEqual((reasons(v), v.feeders["S1-F1"]), ([reason], "on"), checks)

    def test_signatures_alone_execute_the_replay(self):
        v, status = self.run_replay(checks=())
        self.assertEqual((status, v.feeders["S1-F1"]), ("applied", "off"))

    def test_ttl_alone_works_only_late_and_with_a_right_clock(self):
        v, _ = self.run_replay(t=400.0, checks={"ttl"})            # still inside the TTL
        self.assertEqual(v.feeders["S1-F1"], "off")
        v, _ = self.run_replay(t=1800.0, checks={"ttl"})           # 30 minutes later
        self.assertEqual((reasons(v), v.feeders["S1-F1"]), (["expired"], "on"))
        v, _ = self.run_replay(t=1800.0, checks={"ttl"}, clock_offset_s=-3600.0)   # clock set back
        self.assertEqual(v.feeders["S1-F1"], "off")

    def test_a_fast_clock_makes_the_full_verifier_refuse_fresh_commands(self):
        v = verifier(clock_offset_s=3600.0)
        v.on_block(*self.cut, 1.0)
        self.assertEqual((reasons(v), v.feeders["S1-F1"], v.tip_height), (["expired"], "on", 1))


class TestCommitProof(unittest.TestCase):
    def setUp(self):
        self.b1, self.p1 = Chain().add(cmd("S1-F1", "off", 1, 0))

    def test_needs_q_validators(self):
        v = verifier()
        self.assertEqual(v.on_block(self.b1, signed(self.b1, NAMES[:4]), 0.1), "rejected")
        self.assertEqual(v.on_block(self.b1, signed(self.b1, NAMES[:5]), 0.2), "applied")
        self.assertEqual(reasons(v), ["insufficient_quorum"])

    def test_forged_or_prevote_signatures_do_not_count(self):
        outsiders = KeyRing(NAMES, seed=99)
        for proof in (signed(self.b1, keys=outsiders), signed(self.b1, kind=PREVOTE)):
            v = verifier()
            self.assertEqual(v.on_block(self.b1, proof, 0.1), "rejected")
            self.assertEqual(reasons(v), ["insufficient_quorum"])

    def test_a_changed_command_breaks_the_proof(self):
        tampered = dict(self.b1, payload={"reports": [], "commands": [cmd("S1-F1", "on", 1, 0)]})
        v = verifier()
        self.assertEqual(v.on_block(tampered, self.p1, 0.1), "rejected")
        self.assertEqual(reasons(v), ["insufficient_quorum"])

    def test_the_next_block_must_extend_the_trusted_header(self):
        v = verifier()
        v.on_block(self.b1, self.p1, 0.1)
        fork = make_block(CFG, 2, GENESIS, 1, "v1", 0, {"reports": [], "commands": [cmd("S1-F1", "on", 2, 1)]})
        self.assertEqual(v.on_block(fork, signed(fork), 0.2), "rejected")
        self.assertEqual(reasons(v), ["header_mismatch"])

    def test_chain_and_epoch_binding(self):
        drill = Chain(CFG.with_(chain_id="drill-1"))                # same validator keys, another chain
        b, p = drill.add(cmd("S1-F1", "off", 1, 0))
        v = verifier()
        self.assertEqual(v.on_block(b, p, 0.1), "rejected")
        self.assertEqual(reasons(v), ["wrong_chain"])
        no_binding = verifier(checks=ALL_CHECKS - {"binding"})
        self.assertEqual(no_binding.on_block(b, p, 0.1), "applied")  # why the binding check matters
        old = Chain(CFG.with_(epoch=0))
        b, p = old.add(cmd("S1-F1", "off", 1, 0))
        v = verifier()
        v.on_block(b, p, 0.1)
        self.assertEqual(reasons(v), ["wrong_epoch"])


class TestCommandChecks(unittest.TestCase):
    def one(self, command, **kw):
        v = verifier(**kw)
        v.on_block(*Chain().add(command), 0.1)
        return v

    def test_each_reason(self):
        cases = ((cmd("S1-F9", "off", 1, 0), "unknown_feeder"),
                 (cmd("S1-F1", "toggle", 1, 0), "relative_target"),
                 (cmd("S1-F1", "off", 0, 0), "stale_seq"),
                 (cmd("S1-F1", "off", 1, 5), "state_mismatch"),
                 (cmd("S1-F1", "off", 1, 0, expires=0.05), "expired"))
        for command, reason in cases:
            self.assertEqual(reasons(self.one(command)), [reason], reason)

    def test_unknown_feeder_is_refused_with_every_check_off(self):
        self.assertEqual(reasons(self.one(cmd("S1-F9", "off", 1, 0), checks=())), ["unknown_feeder"])

    def test_without_the_target_check_a_toggle_flips(self):
        v = self.one(cmd("S1-F1", "toggle", 1, 0), checks=ALL_CHECKS - {"target"})
        self.assertEqual(v.feeders["S1-F1"], "off")

    def test_policy_check(self):
        v = self.one(cmd("S1-F1", "on", 1, 0), policy_fn=lambda ver, c: c["target"] == "off")
        self.assertEqual(reasons(v), ["policy_violation"])

    def test_logged_reasons_are_the_documented_ones(self):
        self.assertTrue({"wrong_chain", "stale_seq", "expired", "bad_signature"} <= set(REJECT_REASONS))
        with self.assertRaises(ValueError):
            verifier(checks={"orderr"})


class TestCentreSignedCommands(unittest.TestCase):
    """A and B: the dispatch centre signs each command; the same freshness checks apply."""

    def setUp(self):
        self.ring = KeyRing(["centre", "intruder"], CFG.seed)
        self.v = Verifier(CFG, "S1", FEEDERS, keys=self.ring, centre="centre")

    def test_accepts_once_and_rejects_the_replay(self):
        first = sign_command(CFG, self.ring, "centre", cmd("S1-F1", "off", 1, 0))
        self.assertEqual(self.v.on_command(first, 1.0), "applied")
        self.assertEqual(self.v.on_command(sign_command(CFG, self.ring, "centre", cmd("S1-F1", "on", 2, 1)), 301.0),
                         "applied")
        self.assertEqual(self.v.on_command(first, 400.0), "rejected")
        self.assertEqual((reasons(self.v), self.v.feeders["S1-F1"]), (["stale_seq"], "on"))

    def test_signature_issuer_chain_and_address(self):
        c = cmd("S1-F1", "off", 1, 0)
        forged = sign_command(CFG, self.ring, "intruder", c)
        relabelled = dict(forged, issuer="centre")
        tampered = dict(sign_command(CFG, self.ring, "centre", c), command=cmd("S1-F1", "off", 1, 0, expires=99_999.0))
        drill = sign_command(CFG.with_(chain_id="drill-1"), self.ring, "centre", c)
        elsewhere = sign_command(CFG, self.ring, "centre", cmd("S2-F1", "off", 1, 0, sub="S2"))
        for env in (forged, relabelled, tampered, drill, elsewhere):
            self.assertEqual(self.v.on_command(env, 1.0), "rejected")
        self.assertEqual(reasons(self.v), ["bad_signature", "bad_signature", "bad_signature", "wrong_chain",
                                           "unknown_feeder"])
        self.assertEqual(self.v.version, 0)


if __name__ == "__main__":
    unittest.main()
