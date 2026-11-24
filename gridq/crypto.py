"""Simulated signatures: HMAC-SHA256 with one secret key per node (new in gridq).

evsim's signatures were placeholder strings, so nothing could tell a forged or
under-signed command from a real one. The substation verifier needs exactly
that distinction, and the standard library has no Ed25519, so each node gets an
HMAC key derived from the run seed. What this models:

  * a node's signature can only be produced with that node's key;
  * an attacker who compromises k nodes holds k keys and can produce k valid
    signatures, no more (which is what the quorum Q is about);
  * the verifier knows every validator's key, standing in for knowing their
    public keys (asymmetric signatures are not modelled).

Not a security implementation: keys are derived from the seed so that runs are
reproducible.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Dict, Iterable, Mapping


class KeyRing:
    def __init__(self, nodes: Iterable[str], seed: int = 0):
        self._keys: Dict[str, bytes] = {
            n: hashlib.sha256(f"gridq-key-{seed}-{n}".encode()).digest() for n in nodes}

    @property
    def nodes(self) -> tuple:
        return tuple(sorted(self._keys))

    def sign(self, node: str, digest: str) -> str:
        """`node`'s signature over a hex digest (e.g. a block or command hash)."""
        return hmac.new(self._keys[node], digest.encode(), hashlib.sha256).hexdigest()

    def verify(self, node: str, digest: str, sig) -> bool:
        key = self._keys.get(node)
        if key is None or not isinstance(sig, str):
            return False
        good = hmac.new(key, digest.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig)

    def count_valid(self, digest: str, sigs: Mapping[str, str]) -> int:
        """Distinct known nodes whose signature over `digest` verifies (sigs: node -> signature)."""
        return sum(1 for n, s in sigs.items() if self.verify(n, digest, s))
