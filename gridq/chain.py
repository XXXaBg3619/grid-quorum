"""Hash-chained headers and their verification (ported from evsim/mechanisms/m1.py,
BlokcChin@796b43c, where it checked the operator's allocation log).

Each header carries the hash of the previous one. A light node (the substation
verifier) that trusts one header can therefore check that later headers extend
the same chain, and nobody can rewrite an entry without changing every later
hash. verify_chain() makes three checks and reports the first failure:

  1. integrity    every entry's hash recomputes and links to the previous one;
  2. replica      another copy (e.g. another validator's chain) matches entry
                  by entry, which catches a rewrite that re-hashed its own chain;
  3. checkpoints  every trusted hash (e.g. headers a substation already
                  accepted) equals the chain's hash at that height.

evsim's version had the allocation fields hard-coded; here the body is any
dict with a "height" key, so the block format can still change.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .hashing import canonical_hash

GENESIS = "0" * 64


def chain_entry(body: dict, prev: str) -> dict:
    """Header for `body` linked to `prev`: the body plus "prev" and "hash"."""
    if "prev" in body or "hash" in body:
        raise ValueError("body must not contain 'prev' or 'hash'")
    full = {**body, "prev": prev}
    return {**full, "hash": canonical_hash(full)}


def append(chain: List[dict], body: dict) -> dict:
    """Append a header for `body` to `chain` (in place) and return it."""
    e = chain_entry(body, chain[-1]["hash"] if chain else GENESIS)
    chain.append(e)
    return e


def verify_chain(chain: List[dict], replica: Optional[List[dict]] = None,
                 checkpoints: Optional[Dict[int, str]] = None) -> dict:
    """Return {"ok", "reason", "height"} for the first problem found, or ok."""
    prev = GENESIS
    for e in chain:
        body = {k: v for k, v in e.items() if k != "hash"}
        if e.get("prev") != prev or canonical_hash(body) != e.get("hash"):
            return {"ok": False, "reason": "chain broken", "height": e.get("height")}
        prev = e["hash"]
    if replica is not None:
        for a, b in zip(chain, replica):
            if a["hash"] != b["hash"]:
                return {"ok": False, "reason": "differs from replica", "height": a.get("height")}
        if len(chain) != len(replica):
            return {"ok": False, "reason": "length differs from replica",
                    "height": min(len(chain), len(replica))}
    if checkpoints:
        by_height = {e.get("height"): e["hash"] for e in chain}
        for h, expected in sorted(checkpoints.items()):
            if by_height.get(h) != expected:
                return {"ok": False, "reason": "checkpoint mismatch", "height": h}
    return {"ok": True, "reason": "", "height": None}
