"""One live substation: substation.Substation and verifier.Verifier, unchanged, behind a live link.

The substation is the light node at the last hop. It holds the validators'
public keys only, plus its own private key for its state reports. A block is
executed only if its commit proof carries Q validators' signatures, it extends
the last trusted header, and each command for this substation passes the
sequence, state-version and expiry checks; anything else is refused and
logged with the failed check. Without a fresh valid block it simply holds its
feeders as they are (fail-safe hold).

LiveVerifier only adds console lines: one per refused block (the simulator
logs one event per refused command). The substation's state lives in memory,
so a restarted substation container starts again from genesis; keep it
running during a demo (persisting the light node's state is future work).

    GRIDQ_NAME=S1 python -m gridq.live.substation     # keys in /keys (GRIDQ_KEYS)
"""
from __future__ import annotations

import os
from typing import Callable, Optional, Sequence

from ..kernel import PRIO_MESSAGE
from ..lasthop import default_feeders
from ..substation import Substation
from ..verifier import REJECT_REASONS, Verifier
from .runtime import PORT, STATUS_PORT, LiveKernel, LiveLog, LiveNode, UdpTransport, env_config, env_t0, run_forever


class LiveVerifier(Verifier):
    _block_level = False

    def _reject(self, cmds, reason, ctx, t):
        here = f" ({len(cmds)} commands for {self.name})" if cmds else ""
        self.log.note(t, "reject", f"REJECT  block {ctx.get('height')} from {ctx.get('from') or '?'}: {reason}{here}")
        self._block_level = True
        try:
            return super()._reject(cmds, reason, ctx, t)
        finally:
            self._block_level = False

    def _log_reject(self, cmd, reason, ctx, t):
        super()._log_reject(cmd, reason, ctx, t)
        if not self._block_level:
            self.log.note(t, "reject", f"REJECT  {cmd.get('feeder')} -> {cmd.get('target')}: {reason} "
                                       f"(block {ctx.get('height')}, from {ctx.get('from') or '?'})")


class LiveSubLink:
    """What substation.Substation needs from its link (lasthop.LastHop in the simulator)."""

    def __init__(self, cfg, kernel: LiveKernel, transport: UdpTransport, sub_keys, log: LiveLog,
                 validators: Sequence[str]):
        self.cfg, self.kernel, self.transport, self.sub_keys, self.log = cfg, kernel, transport, sub_keys, log
        self.validators = list(validators)

    def timer(self, t: float, fn: Callable, *args) -> None:
        self.kernel.schedule(t, PRIO_MESSAGE, self._fire, fn, args)

    def _fire(self, fn: Callable, args: tuple) -> None:
        fn(*args, self.kernel.now)

    def send_report(self, sub: Substation, report: dict, k: int, t: float) -> None:
        for v in self.validators:
            self.transport.send(v, {"type": "report", "report": report})

    def request_sync(self, sub: Substation, from_height: int, k: int, t: float) -> None:
        msg = {"type": "sync_request", "substation": sub.name, "from": from_height, "k": k}
        for v in self.validators:
            self.transport.send(v, msg)


class SubstationNode(LiveNode):
    def __init__(self, cfg, name: str, *, feeders: Sequence[str], validators: Sequence[str], keys, sub_keys,
                 transport: UdpTransport, kernel: LiveKernel, log: LiveLog):
        super().__init__(kernel, transport, log)
        self.cfg, self.name = cfg, name
        self.link = LiveSubLink(cfg, kernel, transport, sub_keys, log, validators)
        self.sub = Substation(self.link, LiveVerifier(cfg, name, {f: "on" for f in feeders}, keys=keys, log=log))
        self.v = self.sub.v
        self.applied_at: Optional[float] = None

    def start(self) -> None:
        t = self.kernel.now
        self.sub.send_report(t)
        self.link.timer(t + self.cfg.dispatch_period_s / 2.0, self.sub.heartbeat)

    def on_message(self, msg: dict) -> None:
        if msg.get("type") in ("deliver", "sync"):
            tip = self.v.tip_height
            self.sub.on_message(msg, self.kernel.now)
            if self.v.tip_height != tip:
                self.applied_at = self.kernel.now

    def status(self) -> dict:
        v = self.v
        return {
            "role": "substation", "name": self.name, "t": round(self.kernel.now, 3),
            "tip": v.tip_height, "tip_hash": v.tip_hash[:16], "version": v.version, "last_seq": v.last_seq,
            "feeders": dict(sorted(v.feeders.items())),
            "applied_at": None if self.applied_at is None else round(self.applied_at, 3),
            "executed": v.stats["executed"], "blocks": v.stats["blocks"], "duplicates": v.stats["duplicate"],
            "rejected": {r: v.stats[r] for r in REJECT_REASONS if v.stats[r]},
            "rejected_commands": v.stats["rejected_commands"], "waiting": sorted(self.sub.future),
            "net": dict(self.transport.stats), "events": list(self.log.recent)[-15:],
        }


def main() -> None:
    from .keys import read_private, read_public, rings
    cfg = env_config()
    name = os.environ["GRIDQ_NAME"]
    keydir = os.environ.get("GRIDQ_KEYS", "/keys")
    feeders = default_feeders(int(os.environ.get("GRIDQ_SUBSTATIONS", "4")))
    if name not in feeders:
        raise SystemExit(f"{name} is not one of the substations {sorted(feeders)}")
    validators = [f"v{i}" for i in range(cfg.n_validators)]
    keys, sub_keys = rings(read_public(os.path.join(keydir, "public.json")),
                           {name: read_private(os.path.join(keydir, "self.key"))})
    t0 = env_t0()
    transport = UdpTransport(PORT, peers=validators)
    kernel, log = LiveKernel(t0), LiveLog(name, t0)
    node = SubstationNode(cfg, name, feeders=feeders[name], validators=validators, keys=keys, sub_keys=sub_keys,
                          transport=transport, kernel=kernel, log=log)
    log.note(kernel.now, "start", f"substation {name} up: feeders {', '.join(feeders[name])}; executes a block only "
                                  f"with {cfg.quorum} of {cfg.n_validators} validator signatures")
    run_forever(node, STATUS_PORT)


if __name__ == "__main__":
    main()
