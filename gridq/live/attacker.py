"""Someone on the last hop of the live demo: sends blocks straight to the substations.

The attacker reads what anyone on the link could see: committed blocks and
their proofs (from a validator's /chain, standing in for copying deliveries
off the link) and each substation's state (its report). Then:

  replay       resends an old committed block with its real commit proof
               -> stale_seq: at or below the substation's trusted height
  tamper       the newest block moved to the next height with every command
               for a substation flipped, keeping the real proof
               -> insufficient_quorum: the proof signs another block
  forge        a new block at the next height that switches every feeder
               (--target, default off: a blackout), its proof signed with the
               stolen keys given (--keys). Fewer than Q keys
               -> insufficient_quorum; Q keys or more -> executed: the
               substations follow the attacker's chain from then on, the
               common-cause case of experiment 6 (architecture C: one
               organisation runs every site)
  wrong-chain  the same forged block for another island (chain_id island-2)
               -> wrong_chain

    python -m gridq.live.attacker replay
    python -m gridq.live.attacker forge --keys v5,v6
    python -m gridq.live.attacker forge --keys v2,v3,v4,v5,v6
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

from ..consensus.messages import PRECOMMIT, block_id, make_block, make_proof, vote_digest
from ..lasthop import default_feeders
from ..verifier import make_command
from .runtime import PORT, STATUS_PORT, env_config, env_t0

MODES = ("replay", "tamper", "forge", "wrong-chain")


def get_json(host: str, path: str, timeout: float = 1.5):
    with urllib.request.urlopen(f"http://{host.lower()}:{STATUS_PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read())


class Attacker:
    def __init__(self, cfg, validators: Sequence[str], substations: Sequence[str], keydir: Optional[str] = None,
                 stolen: Sequence[str] = (), t0: float = 0.0):
        self.cfg, self.validators, self.substations, self.t0 = cfg, list(validators), list(substations), t0
        self.stolen = list(stolen)
        self.ring = None
        if self.stolen:
            from .keys import read_private, read_public, rings
            public = read_public(os.path.join(keydir, "public.json"))
            self.ring, _ = rings(public, {v: read_private(os.path.join(keydir, "private", f"{v}.key"))
                                          for v in self.stolen})
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ---------- what the link shows ----------
    def chain_from(self, height: int) -> List[list]:
        for v in self.validators:
            try:
                return get_json(v, f"/chain?from={height}")
            except OSError:
                continue
        raise SystemExit("no validator answered; is the demo running?")

    def tip(self) -> int:
        """The newest committed height any validator reports."""
        def last(v):
            try:
                return (get_json(v, "/status", timeout=1.0).get("last") or {}).get("height", 0)
            except (OSError, ValueError):
                return 0
        with ThreadPoolExecutor(max_workers=len(self.validators)) as ex:
            return max(ex.map(last, self.validators), default=0)

    def substation(self, s: str) -> dict:
        return get_json(s, "/status")

    def wait_for_block(self, timeout: float) -> int:
        """Wait until the next block is committed, so a forged block has most of a slot to arrive first."""
        start, h0 = time.time(), self.tip()
        while time.time() - start < timeout:
            time.sleep(0.2)
            h = self.tip()
            if h > h0:
                time.sleep(0.5)                     # let the substations apply it
                return h
        return h0

    # ---------- sending ----------
    def send(self, block: dict, proof: dict, mode: str, targets: Sequence[str]) -> None:
        for s in targets:
            msg = {"type": "deliver", "block": block, "proof": proof, "sender": "attacker",
                   "delivery": f"attack:{mode}:{block.get('height')}:{s}"}
            self.sock.sendto(json.dumps(msg, separators=(",", ":")).encode(), (socket.gethostbyname(s.lower()), PORT))

    # ---------- attacks ----------
    def replay(self, targets, back: int = 2) -> Tuple[dict, dict]:
        h = max(1, self.tip() - back)
        block, proof = self.chain_from(h)[0]
        return block, proof

    def tamper(self, targets) -> Tuple[dict, dict]:
        h = self.tip()
        block, proof = self.chain_from(h)[0]
        cmds = []
        for c in block["payload"]["commands"]:
            cmds.append(dict(c, target="on" if c["target"] == "off" else "off"))
        fake = dict(block, height=h + 1, prev=proof["block"], payload=dict(block["payload"], commands=cmds))
        return fake, proof

    def forge(self, targets, target_state: str = "off", chain_id: Optional[str] = None) -> Tuple[dict, dict]:
        h = self.tip()
        last, proof = self.chain_from(h)[0]
        now = time.time() - self.t0
        cmds = []
        for s in targets:
            st = self.substation(s)
            k = 0
            for f, state in st["feeders"].items():
                if state != target_state:
                    cmds.append(make_command(s, f, target_state, st["last_seq"] + k + 1, st["version"] + k,
                                             round(now + 600.0, 3)))
                    k += 1
        cfg = self.cfg if chain_id is None else self.cfg.with_(chain_id=chain_id)
        proposer = self.stolen[0] if self.stolen else self.validators[0]
        block = make_block(cfg, h + 1, proof["block"], last["slot"] + 1, proposer, 0,
                           {"reports": [], "commands": cmds})
        bid = block_id(block)
        digest = vote_digest(cfg, PRECOMMIT, h + 1, 0, bid)
        sigs = {v: self.ring.sign(v, digest) for v in self.stolen} if self.ring is not None else {}
        return block, make_proof(h + 1, 0, bid, sigs)


def outcome(before: dict, after: dict, bid: str) -> str:
    """What one substation did with the attack block, from its status before and after."""
    grew = {r: n - before.get("rejected", {}).get(r, 0) for r, n in after.get("rejected", {}).items()}
    grew = {r: n for r, n in grew.items() if n > 0}
    if after.get("tip_hash") == bid[:16]:
        return f"EXECUTED the attacker's block {after['tip']}: feeders now {after['feeders']}"
    if grew:
        return "refused: " + ", ".join(f"{r} x{n}" for r, n in sorted(grew.items()))
    return "no change (dropped as a duplicate or not received)"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Attack the live demo's last hop.")
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--keys", default="", help="stolen validator keys for forge / wrong-chain, e.g. v5,v6")
    ap.add_argument("--to", default="", help="substations to attack (default: all)")
    ap.add_argument("--target", choices=("on", "off"), default="off", help="forge: switch every feeder to this")
    ap.add_argument("--keydir", default=os.environ.get("GRIDQ_KEYS", "/keys"))
    args = ap.parse_args(argv)
    cfg = env_config()
    validators = [f"v{i}" for i in range(cfg.n_validators)]
    subs = sorted(default_feeders(int(os.environ.get("GRIDQ_SUBSTATIONS", "4"))))
    targets = [s for s in args.to.split(",") if s] or subs
    stolen = [k for k in args.keys.split(",") if k]
    a = Attacker(cfg, validators, subs, args.keydir, stolen, env_t0())
    if stolen:
        print(f"attacker holds {len(stolen)} validator keys {stolen} (Q = {cfg.quorum})")
    a.wait_for_block(cfg.dispatch_period_s * 3)     # right after a block: most of a slot before the next one
    before = {s: a.substation(s) for s in targets}
    if args.mode == "replay":
        block, proof = a.replay(targets)
    elif args.mode == "tamper":
        block, proof = a.tamper(targets)
    elif args.mode == "forge":
        block, proof = a.forge(targets, args.target)
    else:
        block, proof = a.forge(targets, args.target, chain_id="island-2")
    bid = block_id(block)
    print(f"{args.mode}: block {block['height']} {bid[:12]} (chain {block['chain_id']}) with "
          f"{len(proof['sigs'])} signatures -> {', '.join(targets)}")
    a.send(block, proof, args.mode, targets)
    time.sleep(1.0)
    for s in targets:
        print(f"  {s}: {outcome(before[s], a.substation(s), bid)}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
