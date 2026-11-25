"""Ed25519 keys for live mode, with crypto.KeyRing's interface.

A process holds only its own private key and every validator's and
substation's public key. A substation checks a commit proof with public keys
alone, so forging one takes Q stolen private keys. The simulator's HMAC
KeyRing cannot show that, because whoever can verify there can also sign.

keygen() writes:
  <dir>/public.json          {"validators": {name: hex}, "substations": {name: hex}}
  <dir>/private/<name>.key   the node's 32-byte private key, hex

    python -m gridq.live.keys --out demo/keys
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Dict, Iterable, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

CACHE_MAX = 50000           # remembered verifications (a proof is checked by many paths)


def _pub_hex(key: Ed25519PublicKey) -> str:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


class Ed25519KeyRing:
    """Public keys of a set of nodes, plus the private keys this process holds."""

    def __init__(self, public: Mapping[str, str], private: Optional[Mapping[str, str]] = None):
        self._pub: Dict[str, Ed25519PublicKey] = {
            n: Ed25519PublicKey.from_public_bytes(bytes.fromhex(h)) for n, h in public.items()}
        self._priv: Dict[str, Ed25519PrivateKey] = {}
        for n, h in (private or {}).items():
            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(h))
            if n not in self._pub or _pub_hex(key.public_key()) != public[n]:
                raise ValueError(f"the private key given for {n} does not match its public key")
            self._priv[n] = key
        self._cache: Dict[Tuple[str, str, str], bool] = {}

    @property
    def nodes(self) -> tuple:
        return tuple(sorted(self._pub))

    @property
    def holds(self) -> tuple:
        """The nodes whose private keys this process holds."""
        return tuple(sorted(self._priv))

    def sign(self, node: str, digest: str) -> str:
        key = self._priv.get(node)
        if key is None:
            raise KeyError(f"this process does not hold {node}'s private key")
        return key.sign(digest.encode()).hex()

    def verify(self, node: str, digest: str, sig) -> bool:
        key = self._pub.get(node)
        if key is None or not isinstance(sig, str) or not isinstance(digest, str) or len(sig) != 128:
            return False
        memo = (node, digest, sig)
        hit = self._cache.get(memo)
        if hit is not None:
            return hit
        try:
            key.verify(bytes.fromhex(sig), digest.encode())
            ok = True
        except (InvalidSignature, ValueError):
            ok = False
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[memo] = ok
        return ok

    def count_valid(self, digest: str, sigs: Mapping[str, str]) -> int:
        """Distinct known nodes whose signature over `digest` verifies (sigs: node -> signature)."""
        return sum(1 for n, s in sigs.items() if self.verify(n, digest, s))


# ---------------------------------------------------------------- files

def read_public(path: str) -> Dict[str, Dict[str, str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data.get("validators"), dict) or not isinstance(data.get("substations"), dict):
        raise ValueError(f"{path}: needs 'validators' and 'substations'")
    return data


def read_private(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def rings(public: Mapping[str, Mapping[str, str]], private: Optional[Mapping[str, str]] = None
          ) -> Tuple[Ed25519KeyRing, Ed25519KeyRing]:
    """(validators' ring, substations' ring), each with the private keys in `private` that belong to it.

    The two rings stay apart: a substation's signature never counts toward a quorum."""
    private = dict(private or {})
    unknown = set(private) - set(public["validators"]) - set(public["substations"])
    if unknown:
        raise ValueError(f"private keys for unknown nodes: {sorted(unknown)}")
    pick = lambda group: {n: h for n, h in private.items() if n in public[group]}  # noqa: E731
    return (Ed25519KeyRing(public["validators"], pick("validators")),
            Ed25519KeyRing(public["substations"], pick("substations")))


def new_keys(validators: Iterable[str], substations: Iterable[str]
             ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """Fresh random key pairs: (public part as in public.json, {name: private key hex})."""
    public: Dict[str, Dict[str, str]] = {"validators": {}, "substations": {}}
    private: Dict[str, str] = {}
    for group, names in (("validators", validators), ("substations", substations)):
        for n in names:
            key = Ed25519PrivateKey.generate()
            private[n] = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
            public[group][n] = _pub_hex(key.public_key())
    return public, private


def keygen(out_dir: str, validators: Iterable[str], substations: Iterable[str], force: bool = False) -> dict:
    """Write fresh key pairs to out_dir; returns the public part. Refuses to overwrite unless force."""
    pub_path = os.path.join(out_dir, "public.json")
    if os.path.exists(pub_path) and not force:
        raise FileExistsError(f"{pub_path} exists (use --force to replace every key)")
    os.makedirs(os.path.join(out_dir, "private"), exist_ok=True)
    public, private = new_keys(validators, substations)
    for n, raw in private.items():
        path = os.path.join(out_dir, "private", f"{n}.key")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw + "\n")
    with open(pub_path, "w", encoding="utf-8") as fh:
        json.dump(public, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return public


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Generate Ed25519 keys for the live demo.")
    ap.add_argument("--out", default="keys")
    ap.add_argument("--validators", type=int, default=7)
    ap.add_argument("--substations", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    vs = [f"v{i}" for i in range(args.validators)]
    ss = [f"S{i + 1}" for i in range(args.substations)]
    keygen(args.out, vs, ss, args.force)
    print(f"wrote {len(vs)} validator and {len(ss)} substation key pairs to {args.out}")


if __name__ == "__main__":
    main()
