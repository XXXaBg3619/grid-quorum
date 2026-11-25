"""Live mode (gridq/live/): the Ed25519 key ring, the double-sign guard, and whole islands over
localhost UDP (the Docker demo's nodes, one thread each). Skipped without the cryptography package."""
import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

PERIOD = 0.6                    # seconds per dispatch slot in these tests
DIGEST = "ab" * 32


def wait_until(cond, timeout: float, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


@unittest.skipUnless(HAVE_CRYPTO, "live mode needs the cryptography package")
class TestKeys(unittest.TestCase):
    def setUp(self):
        from gridq.live.keys import new_keys
        self.pub, self.priv = new_keys(["v0", "v1"], ["S1"])

    def test_sign_verify_and_who_can_sign(self):
        from gridq.live.keys import rings
        vk, sk = rings(self.pub, {"v0": self.priv["v0"]})
        sig = vk.sign("v0", DIGEST)
        self.assertTrue(vk.verify("v0", DIGEST, sig))
        self.assertFalse(vk.verify("v1", DIGEST, sig))
        self.assertFalse(vk.verify("v0", "cd" * 32, sig))
        with self.assertRaises(KeyError):
            vk.sign("v1", DIGEST)                    # only its own private key
        self.assertEqual(vk.holds, ("v0",))
        self.assertEqual(sk.holds, ())
        self.assertFalse(vk.verify("S1", DIGEST, sig))    # substations are not validators
        for bad in ("zz" * 64, 123, None, sig[:-2]):
            self.assertFalse(vk.verify("v0", DIGEST, bad))
        self.assertEqual(vk.count_valid(DIGEST, {"v0": sig, "v1": sig, "x": sig}), 1)

    def test_private_key_must_match(self):
        from gridq.live.keys import rings
        with self.assertRaises(ValueError):
            rings(self.pub, {"v0": self.priv["v1"]})
        with self.assertRaises(ValueError):
            rings(self.pub, {"v9": self.priv["v1"]})

    def test_keygen_files(self):
        from gridq.live.keys import keygen, read_private, read_public, rings
        with tempfile.TemporaryDirectory() as d:
            keygen(d, ["v0", "v1"], ["S1"])
            pub = read_public(os.path.join(d, "public.json"))
            path = os.path.join(d, "private", "v1.key")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            vk, _ = rings(pub, {"v1": read_private(path)})
            self.assertTrue(vk.verify("v1", DIGEST, vk.sign("v1", DIGEST)))
            with self.assertRaises(FileExistsError):
                keygen(d, ["v0"], ["S1"])


@unittest.skipUnless(HAVE_CRYPTO, "live mode needs the cryptography package")
class TestGuard(unittest.TestCase):
    def test_never_signs_at_or_below_the_mark_even_after_restart(self):
        from gridq.live.keys import new_keys, rings
        from gridq.live.runtime import LiveKernel, live_config
        from gridq.live.validator import LiveCommittee
        cfg = live_config(PERIOD)
        names = [f"v{i}" for i in range(7)]
        pub, priv = new_keys(names, [])
        keys, _ = rings(pub, {"v0": priv["v0"]})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "guard.json")
            com = LiveCommittee(cfg, LiveKernel(time.time()), None, local="v0", guard_path=path, keys=keys,
                                names=names)
            self.assertTrue(com.may_sign(3, 0, "proposal"))
            self.assertTrue(com.may_sign(3, 0, "prevote"))
            self.assertFalse(com.may_sign(3, 0, "prevote"))        # the same step again
            self.assertFalse(com.may_sign(3, 0, "proposal"))       # an earlier step
            self.assertTrue(com.may_sign(3, 1, "prevote"))
            again = LiveCommittee(cfg, LiveKernel(time.time()), None, local="v0", guard_path=path, keys=keys,
                                  names=names)
            self.assertEqual(again.signed, (3, 1, 1))
            self.assertFalse(again.may_sign(2, 5, "precommit"))
            self.assertFalse(again.may_sign(3, 1, "prevote"))
            self.assertTrue(again.may_sign(3, 1, "precommit"))
            self.assertEqual(again.stats["guard_refused"], 2)


@unittest.skipUnless(HAVE_CRYPTO, "live mode needs the cryptography package")
class TestIsland(unittest.TestCase):
    """Seven validators and four substations, each on its own thread and UDP port."""

    def cfg(self):
        from gridq.live.runtime import live_config
        return live_config(PERIOD)

    def test_honest_island_commits_and_substations_execute(self):
        from gridq.live.cluster import Cluster
        with Cluster(self.cfg()) as c:
            ok = wait_until(lambda: min(c.heights().values()) >= 4
                            and min(s.v.tip_height for s in c.substations.values()) >= 4, 20 * PERIOD)
        self.assertTrue(ok, c.heights())
        self.assertEqual(c.conflicting_heights(), [])
        for s in c.substations.values():
            self.assertGreaterEqual(s.v.stats["executed"], 6)
            self.assertEqual(s.v.stats["rejected_commands"], 0)
        chain = c.validators["v0"].node.chain
        self.assertTrue(any(h["payload"]["reports"] for h in chain))   # signed state reports reach the chain

    def test_f_offline_still_commits_more_than_f_holds(self):
        from gridq.live.cluster import Cluster
        with Cluster(self.cfg(), skip=["v5", "v6"]) as c:
            ok = wait_until(lambda: min(c.heights().values()) >= 3, 25 * PERIOD)
        self.assertTrue(ok, c.heights())
        self.assertEqual(c.conflicting_heights(), [])
        with Cluster(self.cfg(), skip=["v4", "v5", "v6"]) as c:
            time.sleep(5 * PERIOD)
            heights = c.heights()
            tips = [s.v.tip_height for s in c.substations.values()]
            feeders = [dict(s.v.feeders) for s in c.substations.values()]
        self.assertEqual(set(heights.values()), {0})       # 4 < Q: nothing is committed
        self.assertEqual(tips, [0, 0, 0, 0])               # and the substations hold their state
        self.assertTrue(all(set(f.values()) == {"on"} for f in feeders))

    def test_equivocating_validator_is_caught_and_cannot_split_the_chain(self):
        from gridq.faults import Equivocate
        from gridq.live.cluster import Cluster
        with Cluster(self.cfg(), faults={"v3": Equivocate(["v3"])}) as c:
            caught = lambda: sum(1 for n, v in c.validators.items()           # noqa: E731
                                 if n != "v3" and any(e["offender"] == "v3" for e in v.log.evidence))
            ok = wait_until(lambda: caught() >= 4 and min(c.heights().values()) >= 5, 40 * PERIOD)
        self.assertTrue(ok, (caught(), c.heights()))
        self.assertTrue(c.validators["v3"].fault.pairs)        # it did send two blocks in some round
        self.assertEqual(c.conflicting_heights(), [])

    def test_last_hop_attacks_are_refused(self):
        from gridq.consensus.messages import PRECOMMIT, block_id, make_block, make_proof, vote_digest
        from gridq.live.cluster import Cluster
        from gridq.live.keys import rings
        with Cluster(self.cfg()) as c:
            s1 = c.substations["S1"]
            self.assertTrue(wait_until(lambda: s1.v.tip_height >= 4, 20 * PERIOD))
            v0 = c.validators["v0"].node
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            addr = ("127.0.0.1", c.transports["S1"].port)

            def send(block, proof):
                sock.sendto(json.dumps({"type": "deliver", "block": block, "proof": proof,
                                        "sender": "attacker"}).encode(), addr)

            old = {k: v for k, v in v0.chain[1].items() if k != "hash"}
            send(old, v0.proofs[1])                                  # replay of block 2
            h = s1.v.tip_height
            tip = {k: v for k, v in v0.chain[h - 1].items() if k != "hash"}
            moved = dict(tip, height=h + 1, prev=v0.chain[h - 1]["hash"])
            send(moved, v0.proofs[h - 1])                            # a real proof on another block
            stolen, _ = rings(c.public, {"v5": c.private["v5"], "v6": c.private["v6"]})
            forged = make_block(c.cfg, h + 1, v0.chain[h - 1]["hash"], tip["slot"] + 1, "v5", 0,
                                {"reports": [], "commands": []})
            digest = vote_digest(c.cfg, PRECOMMIT, h + 1, 0, block_id(forged))
            send(forged, make_proof(h + 1, 0, block_id(forged), {v: stolen.sign(v, digest) for v in ("v5", "v6")}))
            other = c.cfg.with_(chain_id="island-2")
            send(make_block(other, h + 1, v0.chain[h - 1]["hash"], tip["slot"] + 1, "v5", 0, {}), {})
            self.assertTrue(wait_until(lambda: s1.v.stats["wrong_chain"] >= 1, 2.0))
            time.sleep(0.2)
            stats = dict(s1.v.stats)
            sock.close()
        self.assertGreaterEqual(stats["stale_seq"], 1)
        self.assertGreaterEqual(stats["insufficient_quorum"], 2)
        self.assertEqual(stats["wrong_chain"], 1)
        self.assertEqual(c.conflicting_heights(), [])


if __name__ == "__main__":
    unittest.main()
