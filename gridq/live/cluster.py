"""A whole live island in one process: every node on its own thread and UDP port on localhost.

The same nodes as the Docker demo, without Docker: for the tests and for a
quick look on a machine without Docker. Keys are generated in memory.

    python -m gridq.live.cluster --seconds 40 --period 4
"""
from __future__ import annotations

import argparse
import threading
import time
from typing import Dict, Iterable, Mapping, Optional

from ..faults import Fault
from ..lasthop import default_feeders
from .keys import new_keys, rings
from .runtime import LiveKernel, LiveLog, UdpTransport, live_config, serve
from .substation import SubstationNode
from .validator import ValidatorNode


class Cluster:
    """n validators and the substations of default_feeders(n_substations), wired over 127.0.0.1.

    skip: validators that are never started (offline from the start).
    faults: validator -> Fault. t0: shared start time (default: now)."""

    def __init__(self, cfg, *, n_substations: int = 4, skip: Iterable[str] = (),
                 faults: Optional[Mapping[str, Fault]] = None, t0: Optional[float] = None, echo: bool = False):
        self.cfg = cfg
        self.t0 = time.time() if t0 is None else t0
        self.names = [f"v{i}" for i in range(cfg.n_validators)]
        self.feeders = default_feeders(n_substations)
        self.public, self.private = new_keys(self.names, sorted(self.feeders))
        self.skip = set(skip)
        transports = {n: UdpTransport(0, host="127.0.0.1") for n in self.names + sorted(self.feeders)}
        addresses = {n: ("127.0.0.1", tr.port) for n, tr in transports.items()}
        for tr in transports.values():
            tr.addresses.update(addresses)
        self.transports = transports
        self.validators: Dict[str, ValidatorNode] = {}
        self.substations: Dict[str, SubstationNode] = {}
        for n in self.names:
            keys, sub_keys = rings(self.public, {n: self.private[n]})
            self.validators[n] = ValidatorNode(
                cfg, n, names=self.names, feeders=self.feeders, keys=keys, sub_keys=sub_keys,
                transport=transports[n], kernel=LiveKernel(self.t0), log=LiveLog(n, self.t0, echo=echo),
                fault=(faults or {}).get(n))
        for s, fs in self.feeders.items():
            keys, sub_keys = rings(self.public, {s: self.private[s]})
            self.substations[s] = SubstationNode(
                cfg, s, feeders=fs, validators=self.names, keys=keys, sub_keys=sub_keys, transport=transports[s],
                kernel=LiveKernel(self.t0), log=LiveLog(s, self.t0, echo=echo))
        self._stop = threading.Event()
        self._threads = []

    def start(self) -> "Cluster":
        for name, node in list(self.validators.items()) + list(self.substations.items()):
            if name in self.skip:
                continue
            th = threading.Thread(target=serve, args=(node,), kwargs={"stop": self._stop}, daemon=True, name=name)
            th.start()
            self._threads.append(th)
        return self

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=5.0)
        for tr in self.transports.values():
            tr.close()

    def __enter__(self) -> "Cluster":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- results ----------
    def running(self):
        return [n for n in self.names if n not in self.skip]

    def heights(self) -> Dict[str, int]:
        return {n: len(self.validators[n].node.chain) for n in self.running()}

    def conflicting_heights(self):
        """Heights at which two running validators committed different blocks."""
        seen: Dict[int, set] = {}
        for n in self.running():
            for e in self.validators[n].node.chain:
                seen.setdefault(e["height"], set()).add(e["hash"])
        return sorted(h for h, hs in seen.items() if len(hs) > 1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Run a live island in one process (no Docker).")
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--period", type=float, default=4.0)
    ap.add_argument("--offline", default="", help="validators never started, e.g. v5,v6")
    args = ap.parse_args(argv)
    cfg = live_config(args.period)
    skip = [x for x in args.offline.split(",") if x]
    with Cluster(cfg, skip=skip, echo=True) as c:
        time.sleep(args.seconds)
    print(f"heights {c.heights()}; conflicting heights {c.conflicting_heights()}")
    for s, node in c.substations.items():
        print(f"{s}: tip {node.v.tip_height}, executed {node.v.stats['executed']}, feeders {node.v.feeders}")


if __name__ == "__main__":
    main()
