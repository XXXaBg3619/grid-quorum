"""Blocks, votes and commit proofs: what the consensus produces and what the
substation verifier checks (new in gridq).

Every signature covers a digest that names the chain, the epoch, the height,
the round and the block, so it cannot be replayed on another chain, epoch,
height, round or block. A commit proof is the precommit signatures of at least
Q distinct validators over one block; verify_commit() is the check a light node
makes on it.
"""
from __future__ import annotations

from typing import Mapping, Optional

from ..hashing import canonical_hash

PREVOTE, PRECOMMIT = "prevote", "precommit"


def make_block(cfg, height: int, prev: str, slot: int, proposer: str, rnd: int, payload) -> dict:
    """Header fields plus the application payload (the dispatch commands, once sim.py exists).

    slot is the dispatch slot the block was proposed in; round and proposer
    record where it was first proposed (a later round may re-propose it)."""
    return {"chain_id": cfg.chain_id, "epoch": cfg.epoch, "height": height, "prev": prev,
            "slot": slot, "proposer": proposer, "round": rnd, "payload": payload}


def block_id(block) -> str:
    """The block hash. A block plus {"hash": block_id(block)} is a chain.py header."""
    return canonical_hash(block)


def proposal_digest(cfg, height: int, rnd: int, bid: str, valid_round: int) -> str:
    """What a proposer signs: this block for this height and round."""
    return canonical_hash({"type": "proposal", "chain_id": cfg.chain_id, "epoch": cfg.epoch,
                           "height": height, "round": rnd, "block": bid, "valid_round": valid_round})


def vote_digest(cfg, kind: str, height: int, rnd: int, bid: Optional[str]) -> str:
    """What a prevote or precommit signs; bid None is a vote for nil."""
    return canonical_hash({"type": kind, "chain_id": cfg.chain_id, "epoch": cfg.epoch,
                           "height": height, "round": rnd, "block": bid})


def make_proof(height: int, rnd: int, bid: str, sigs: Mapping[str, str]) -> dict:
    """A commit proof: precommit signatures (validator -> signature) for one block."""
    return {"height": height, "round": rnd, "block": bid, "sigs": dict(sigs)}


def verify_commit(keys, cfg, block, proof) -> str:
    """'' if `proof` commits `block`, otherwise the first failed check, named as in
    verifier.REJECT_REASONS: wrong_chain, wrong_epoch, or insufficient_quorum
    (the signatures are not Q distinct validators' precommits for this block)."""
    if not isinstance(block, dict) or block.get("chain_id") != cfg.chain_id:
        return "wrong_chain"
    if block.get("epoch") != cfg.epoch:
        return "wrong_epoch"
    if (not isinstance(proof, dict) or proof.get("block") != block_id(block)
            or proof.get("height") != block.get("height") or not isinstance(proof.get("round"), int)
            or not isinstance(proof.get("sigs"), Mapping)):
        return "insufficient_quorum"
    digest = vote_digest(cfg, PRECOMMIT, block["height"], proof["round"], proof["block"])
    return "" if keys.count_valid(digest, proof["sigs"]) >= cfg.quorum else "insufficient_quorum"
