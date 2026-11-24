"""Message-level consensus (gridq/consensus): the two checks of docs/DESIGN.md
section 7 (round 0 against theory under iid loss, no conflicting commits), plus
offline validators, partitions, equivocation, locking and catch-up."""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.chain import verify_chain  # noqa: E402
from gridq.config import GridConfig  # noqa: E402
from gridq.consensus import PREVOTE, block_id, run_committee, verify_commit, vote_digest  # noqa: E402
from gridq.events import EventKind  # noqa: E402
from gridq.faults import Fault  # noqa: E402
from gridq.network import Network  # noqa: E402
from gridq.theory import commit_latency_bound, round_outcome, round_success_prob  # noqa: E402

V = [f"v{i}" for i in range(7)]
SLOW_SLOTS = 3600.0       # dispatch slot long enough that no height runs into the next one


class Down(Fault):
    """These validators are down (send and receive nothing) during [start, end)."""
    label = "down-test"

    def __init__(self, nodes, start=0.0, end=math.inf):
        super().__init__(1.0)
        self.nodes, self.start, self.end = set(nodes), start, end

    def node_up(self, sim, node, t):
        return not (node in self.nodes and self.start <= t < self.end)


class Equivocate(Fault):
    """Compromised validators double-sign (the seed of experiment 1): as proposer they
    send block A to one group and block B to the other, and as voters in such a round
    they vote A to the first group and B to the second."""
    label = "equivocate-test"

    def __init__(self, bad, groups=None, seed=0):
        super().__init__(1.0)
        self.bad, self.groups, self.seed = set(bad), groups, seed
        self.pairs = {}

    def compromised(self, sim):
        return set(self.bad)

    def _split(self, height, rnd, recipients):
        if self.groups:
            return self.groups
        honest = [r for r in recipients if r not in self.bad]
        random.Random(f"split-{self.seed}-{height}-{rnd}").shuffle(honest)
        k = len(honest) // 2
        return set(honest[:k]) | self.bad, set(honest[k:])

    def proposal(self, sim, node, height, rnd, block, recipients):
        if node not in self.bad:
            return super().proposal(sim, node, height, rnd, block, recipients)
        g1, g2 = self._split(height, rnd, recipients)
        other = dict(block, payload={"forged": rnd})
        self.pairs[(height, rnd)] = (block_id(block), block_id(other))
        return {r: (block if r in g1 else other) for r in recipients if r in g1 | g2}

    def vote(self, sim, node, kind, height, rnd, value, recipients):
        pair = self.pairs.get((height, rnd))
        if node not in self.bad or pair is None:
            return super().vote(sim, node, kind, height, rnd, value, recipients)
        g1, g2 = self._split(height, rnd, recipients)
        return {r: (pair[0] if r in g1 else pair[1]) for r in recipients if r in g1 | g2}


class DropNet(Network):
    """The normal network, except that drop(kind, height, round, src, dst) messages are lost."""

    def __init__(self, cfg, drop):
        super().__init__(cfg)
        self.drop = drop

    def deliver(self, msg_id, src, dst, t):
        kind, h, r, _ = msg_id.split(":")
        if src != dst and self.drop(kind, int(h), int(r), src, dst):
            return None
        return super().deliver(msg_id, src, dst, t)


def clean_heights(com):
    """Heights whose round 0 every validator started at the same moment."""
    n = len(com.names)
    return [h for h, st in sorted(com.starts.items())
            if len(st) == n and max(st.values()) - min(st.values()) < 1e-9]


def committed_in_round0(com, h):
    d = com.decisions.get(h, {})
    return bool(d) and min(c["round"] for c in d.values()) == 0


def same_chains(test, com, nodes=None):
    ref = com.nodes[com.names[0]].chain
    for n in nodes or com.names:
        r = verify_chain(com.nodes[n].chain, ref)
        test.assertTrue(r["ok"], (n, r))


class TestHonest(unittest.TestCase):
    def test_no_loss_commits_every_height_in_round0_within_three_hops(self):
        cfg = GridConfig()
        com = run_committee(cfg, heights=12)
        fc = com.first_commits()
        self.assertEqual(sorted(fc), list(range(1, 13)))
        self.assertTrue(all(c["round"] == 0 for c in fc.values()))
        self.assertLessEqual(max(c["latency"] for c in fc.values()), commit_latency_bound(cfg) + 1e-9)
        same_chains(self, com)
        self.assertEqual(len(com.nodes["v3"].chain), 12)
        # (n - 1) + 2n(n - 1) messages between validators per round, plus 2n + 1 to themselves
        n = cfg.n_validators
        self.assertEqual(com.stats["sent"], 12 * (n + 2 * n * n))
        self.assertEqual(com.stats["catch_up"], 0)

    def test_round0_equals_theory_model_on_the_same_draws(self):
        # deterministic: at every height, round 0 commits exactly when theory.round_outcome
        # says it should, given which of that height's messages the network lost
        for n, p, seed in [(7, 0.15, 1), (4, 0.25, 2)]:
            com = run_committee(GridConfig(n_validators=n, link_loss_p=p, seed=seed,
                                           dispatch_period_s=SLOW_SLOTS), heights=60)
            hs = clean_heights(com)
            self.assertGreater(len(hs), 50)
            names = com.names
            for h in hs:
                got = (lambda kind, i, j, h=h: com.net.deliver(f"{kind}:{h}:0:{names[i]}", names[i],
                                                               names[j], 0.0) is not None)
                expect = round_outcome(n, com.q, names.index(com.proposer(h, 0)), got)
                self.assertEqual(committed_in_round0(com, h), expect, (n, p, h))

    def test_round0_share_matches_round_success_prob(self):
        for n, p, runs, heights in [(7, 0.1, 2, 120), (4, 0.2, 3, 150)]:
            ok = total = 0
            for run in range(runs):
                com = run_committee(GridConfig(n_validators=n, link_loss_p=p, seed=7,
                                               dispatch_period_s=SLOW_SLOTS), run=run, heights=heights)
                hs = clean_heights(com)
                total += len(hs)
                ok += sum(committed_in_round0(com, h) for h in hs)
            th = round_success_prob(n, 2 * n // 3 + 1, p)
            se = math.sqrt(th * (1 - th) / total)
            self.assertLess(abs(ok / total - th), 4 * se + 0.01, (n, p, ok, total, th))

    def test_lossy_runs_stay_safe_and_every_height_commits(self):
        for p in (0.1, 0.25):
            com = run_committee(GridConfig(link_loss_p=p, seed=3), heights=25, drain_s=3600.0)
            self.assertEqual(com.conflicting_heights(), [], p)
            self.assertEqual(com.undecided_heights(), [], p)
            same_chains(self, com)

    def test_deterministic_and_log_chained(self):
        cfg = GridConfig(link_loss_p=0.1)
        a, b = run_committee(cfg, heights=5), run_committee(cfg, heights=5)
        self.assertEqual(a.log.head, b.log.head)
        self.assertNotEqual(a.log.head, run_committee(cfg, run=1, heights=5).log.head)
        self.assertTrue(a.log.verify_chain())


class TestCommitProof(unittest.TestCase):
    def setUp(self):
        self.cfg = GridConfig()
        self.com = run_committee(self.cfg, heights=2)
        v0 = self.com.nodes["v0"]
        self.block = {k: x for k, x in v0.chain[0].items() if k != "hash"}
        self.proof = v0.proofs[0]
        self.keys = self.com.keys

    def check(self, block=None, proof=None, cfg=None):
        return verify_commit(self.keys, cfg or self.cfg, block or self.block, proof or self.proof)

    def test_a_committed_block_verifies(self):
        self.assertEqual(self.check(), "")
        self.assertEqual(self.proof["block"], block_id(self.block))
        self.assertEqual(len(self.proof["sigs"]), self.cfg.quorum)

    def test_what_a_substation_must_refuse(self):
        q = self.cfg.quorum
        sigs = self.proof["sigs"]
        self.assertEqual(self.check(proof=dict(self.proof, sigs=dict(list(sigs.items())[:q - 1]))),
                         "insufficient_quorum")
        forged = dict(sigs)
        forged[next(iter(forged))] = "0" * 64                   # one signature not made with that key
        self.assertEqual(self.check(proof=dict(self.proof, sigs=forged)), "insufficient_quorum")
        self.assertEqual(self.check(block=dict(self.block, payload={"evil": 1})), "insufficient_quorum")
        self.assertEqual(self.check(cfg=self.cfg.with_(chain_id="island-2")), "wrong_chain")
        self.assertEqual(self.check(cfg=self.cfg.with_(epoch=2)), "wrong_epoch")
        prevotes = {n: self.keys.sign(n, vote_digest(self.cfg, PREVOTE, 1, self.proof["round"], self.proof["block"]))
                    for n in self.com.names}                    # Q prevotes are not a commit
        self.assertEqual(self.check(proof=dict(self.proof, sigs=prevotes)), "insufficient_quorum")


class TestOffline(unittest.TestCase):
    def test_f_down_keeps_committing_within_the_latency_bound(self):
        cfg = GridConfig()
        down = {"v5", "v6"}
        com = run_committee(cfg, fault=Down(down), heights=14)
        up = [n for n in com.names if n not in down]
        self.assertEqual(com.undecided_heights(up), [])
        same_chains(self, com, up)
        silent_counts = set()
        for h, c in com.first_commits().items():
            k = 0
            while com.proposer(h, k) in down:                   # proposers that are down
                k += 1
            silent_counts.add(k)
            self.assertEqual(c["round"], k, h)
            self.assertLessEqual(c["latency"], commit_latency_bound(cfg, k) + 1e-9, h)
            if k:
                self.assertGreater(c["latency"], commit_latency_bound(cfg, k - 1), h)
        self.assertEqual(silent_counts, {0, 1, 2})

    def test_more_than_f_down_commits_nothing(self):
        com = run_committee(GridConfig(), fault=Down({"v4", "v5", "v6"}), heights=4)
        self.assertEqual(dict(com.decisions), {})
        self.assertTrue(list(com.log.of_kind(EventKind.ROUND_TIMEOUT)))

    def test_node_back_from_an_outage_catches_up(self):
        com = run_committee(GridConfig(), fault=Down({"v3"}, 650.0, 2150.0), heights=10)
        v3 = com.nodes["v3"]
        self.assertEqual(len(v3.chain), 10)
        same_chains(self, com)
        self.assertEqual({com.decisions[h]["v3"]["via"] for h in range(4, 9)}, {"sync"})


class TestPartition(unittest.TestCase):
    def test_quorum_side_keeps_committing_and_the_rest_catches_up(self):
        com = run_committee(GridConfig(), heights=10, partitions=[(450.0, 1950.0, [V[:5], V[5:]])])
        during = [h for h, c in com.first_commits().items() if 450.0 <= c["t"] < 1950.0]
        self.assertEqual(len(during), 5)                        # Q = 5 nodes on one side
        for h in during:
            self.assertTrue(all(com.decisions[h][n]["t"] >= 1950.0 for n in V[5:]))
        self.assertEqual(com.conflicting_heights(), [])
        self.assertEqual(com.undecided_heights(), [])
        same_chains(self, com)

    def test_no_quorum_anywhere_halts_then_recovers(self):
        com = run_committee(GridConfig(), heights=10, partitions=[(450.0, 1950.0, [V[:4], V[4:]])])
        times = [c["t"] for d in com.decisions.values() for c in d.values()]
        self.assertFalse([t for t in times if 450.0 <= t < 1950.0])
        self.assertTrue([t for t in times if t >= 1950.0])
        self.assertEqual(com.conflicting_heights(), [])
        self.assertEqual(com.undecided_heights(), [])
        same_chains(self, com)


class TestByzantine(unittest.TestCase):
    def test_equivocation_by_f_validators_never_commits_two_blocks(self):
        for seed, p in [(1, 0.0), (2, 0.1), (3, 0.2)]:
            com = run_committee(GridConfig(link_loss_p=p, seed=seed), fault=Equivocate({"v1", "v4"}, seed=seed),
                                heights=20, drain_s=3600.0)
            self.assertEqual(com.conflicting_heights(), [], (seed, p))
            self.assertEqual(com.undecided_heights(com.honest()), [], (seed, p))
            same_chains(self, com, com.honest())
            self.assertTrue(list(com.log.of_kind(EventKind.FAULT_ACTION)))

    def test_equivocation_by_more_than_f_can_commit_two_blocks(self):
        bad = {"v0", "v1", "v2"}                                # v1 proposes height 1
        com = run_committee(GridConfig(), fault=Equivocate(bad, groups=({"v3", "v4"} | bad, {"v5", "v6"})),
                            heights=1)
        self.assertEqual(com.conflicting_heights(), [1])
        self.assertNotEqual(com.decisions[1]["v3"]["block"], com.decisions[1]["v5"]["block"])

    def test_conflicting_votes_are_logged_as_evidence_once(self):
        com = run_committee(GridConfig(), heights=1)
        v0 = com.nodes["v0"]
        for b in "abc":
            v0.add_vote(PREVOTE, 0, "v3", b * 64, "sig")
        ev = list(com.log.of_kind(EventKind.EVIDENCE))
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].data["offender"], ev[0].data["kind"]), ("v3", PREVOTE))


class TestLocking(unittest.TestCase):
    def test_lock_holds_and_the_proof_of_lock_lets_others_accept(self):
        # Height 1. Round 0 (proposer v1): everybody prevotes block A, but the prevotes
        # to U are lost, so only L = {v0, v1, v3, v4} see the polka, lock A and
        # precommit it: 4 < Q, nothing commits. Round 1 (proposer v2, in U) proposes a
        # new block, which the locked nodes refuse. Round 2 (proposer v3) re-proposes A
        # with the round-0 prevote signatures; U never saw those prevotes but can check
        # the certificate, so A commits in round 2.
        cfg = GridConfig()
        U = {"v2", "v5", "v6"}
        net = DropNet(cfg, lambda kind, h, r, src, dst: (kind, h, r) == ("prevote", 1, 0) and dst in U)
        com = run_committee(cfg, net=net, heights=1)

        def sent(kind, rnd):
            return {e.actor.split(":")[1]: e.data for e in com.log.of_kind(kind)
                    if e.data["height"] == 1 and e.data["round"] == rnd}

        a = sent(EventKind.PROPOSAL, 0)["v1"]["block"]
        first = com.first_commits()[1]
        self.assertEqual((first["round"], first["block"]), (2, a))
        self.assertNotEqual(sent(EventKind.PROPOSAL, 1)["v2"]["block"], a)
        self.assertEqual({n: d["block"] for n, d in sent(EventKind.PREVOTE, 1).items() if n not in U},
                         {n: None for n in V if n not in U})
        self.assertEqual(sent(EventKind.PROPOSAL, 2)["v3"]["valid_round"], 0)
        self.assertEqual({sent(EventKind.PREVOTE, 2)[n]["block"] for n in U}, {a})
        self.assertEqual(com.conflicting_heights(), [])


if __name__ == "__main__":
    unittest.main()
