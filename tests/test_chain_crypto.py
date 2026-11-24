"""Header chain (ported from evsim tests/test_m1_log.py) and simulated signatures."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.chain import GENESIS, append, chain_entry, verify_chain  # noqa: E402
from gridq.crypto import KeyRing  # noqa: E402
from gridq.hashing import canonical_hash  # noqa: E402


def build_chain(n: int = 6):
    chain = []
    for h in range(1, n + 1):
        append(chain, {"height": h, "block": f"b{h}", "commands": [f"c{h}a", f"c{h}b"]})
    return chain


class TestHeaderChain(unittest.TestCase):
    def test_honest_chain_verifies(self):
        chain = build_chain()
        checkpoints = {2: chain[1]["hash"], 6: chain[5]["hash"]}
        self.assertTrue(verify_chain(chain, copy.deepcopy(chain), checkpoints)["ok"])

    def test_naive_rewrite_breaks_chain(self):
        chain = build_chain()
        chain[2]["commands"].remove("c3a")
        r = verify_chain(chain)
        self.assertEqual((r["ok"], r["reason"], r["height"]), (False, "chain broken", 3))

    def test_rehashed_rewrite_caught_by_replica_and_checkpoints(self):
        # the attacker removes a command and re-hashes every later header of its own copy
        honest = build_chain()
        forged = copy.deepcopy(honest)
        forged[2]["commands"].remove("c3a")
        prev = forged[1]["hash"]
        for e in forged[2:]:
            e["prev"] = prev
            e["hash"] = canonical_hash({k: v for k, v in e.items() if k != "hash"})
            prev = e["hash"]
        self.assertTrue(verify_chain(forged)["ok"])                     # internally consistent ...
        self.assertEqual(verify_chain(forged, honest)["reason"], "differs from replica")
        self.assertEqual(verify_chain(forged, None, {4: honest[3]["hash"]})["reason"], "checkpoint mismatch")

    def test_length_mismatch(self):
        chain = build_chain()
        self.assertEqual(verify_chain(chain, chain[:-1])["reason"], "length differs from replica")

    def test_entry_links_and_reserved_keys(self):
        e = chain_entry({"height": 1}, "f" * 64)
        self.assertEqual(e["prev"], "f" * 64)
        self.assertEqual(build_chain(1)[0]["prev"], GENESIS)
        with self.assertRaises(ValueError):
            chain_entry({"height": 1, "hash": "x"}, GENESIS)


class TestKeyRing(unittest.TestCase):
    def setUp(self):
        self.nodes = [f"v{i}" for i in range(7)]
        self.ring = KeyRing(self.nodes, seed=1)
        self.digest = canonical_hash({"height": 3, "block": "abc"})

    def test_sign_and_verify(self):
        s = self.ring.sign("v2", self.digest)
        self.assertTrue(self.ring.verify("v2", self.digest, s))
        self.assertFalse(self.ring.verify("v3", self.digest, s))                 # another node's signature
        self.assertFalse(self.ring.verify("v2", canonical_hash("other"), s))     # another block
        self.assertFalse(self.ring.verify("mallory", self.digest, s))            # not a validator
        self.assertFalse(self.ring.verify("v2", self.digest, None))

    def test_deterministic_per_seed(self):
        self.assertEqual(KeyRing(self.nodes, seed=1).sign("v0", self.digest), self.ring.sign("v0", self.digest))
        self.assertNotEqual(KeyRing(self.nodes, seed=2).sign("v0", self.digest), self.ring.sign("v0", self.digest))

    def test_count_valid_needs_distinct_real_signers(self):
        proof = {n: self.ring.sign(n, self.digest) for n in self.nodes[:4]}
        self.assertEqual(self.ring.count_valid(self.digest, proof), 4)          # below Q = 5
        proof["v4"] = "forged"
        self.assertEqual(self.ring.count_valid(self.digest, proof), 4)
        proof["v4"] = self.ring.sign("v4", self.digest)
        self.assertEqual(self.ring.count_valid(self.digest, proof), 5)


if __name__ == "__main__":
    unittest.main()
