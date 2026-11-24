"""Canonical hashing (ported from evsim/model.py, BlokcChin@796b43c).

canonical_hash() is sha256 over canonical JSON (sorted keys, no spaces), so
the same object hashes the same on every node. Used for command ids, block and
header hashes, state versions and the event-log chain.
"""
from __future__ import annotations

import hashlib
import json


def canonical_json(obj) -> bytes:
    """Canonical JSON bytes: sorted keys, no spaces, non-JSON values as str()."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def canonical_hash(obj) -> str:
    """sha256 hex digest over canonical JSON."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def short_id(obj, n: int = 16) -> str:
    """First n hex digits of canonical_hash(obj): deterministic ids and tie-breakers."""
    return canonical_hash(obj)[:n]
