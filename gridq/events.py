"""Event log (ported from evsim/events.py, BlokcChin@796b43c; event kinds replaced).

One append-only log per run. Every event has a `visibility`:

* "public" - something a participant could observe or record (proposals,
             votes, commits, commands, substation decisions, switching, UFLS).
             Architectures and verifiers may use only these.
* "truth"  - simulator ground truth (what an injected fault did, lost
             messages). Used only by metrics, never by an architecture, so a
             detection result cannot peek at the answer.

Each event is hash-chained (prev_hash -> hash), so any run can be verified and
compared. Consensus votes are logged once, when cast, not once per delivery.
Export is JSON Lines, one event per line.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, List, Optional

from .hashing import canonical_hash


class EventKind(str, Enum):
    # consensus (C, D)
    PROPOSAL = "proposal"                    # a proposer's block for (height, round)
    PREVOTE = "prevote"
    PRECOMMIT = "precommit"
    COMMIT = "commit"                        # a node saw Q precommits for one block (data: commit proof)
    ROUND_TIMEOUT = "round_timeout"          # a step timer fired (data["step"]): vote nil or move to the next round
    EVIDENCE = "evidence"                    # two conflicting signed messages from one validator (double signing)
    # dispatch path (all architectures)
    COMMAND_ISSUED = "command_issued"        # dispatch centre (A, B) or committee (C, D) issued a command
    COMMAND_RECEIVED = "command_received"    # a substation got a command
    COMMAND_ACCEPTED = "command_accepted"    # the substation verified and executed it
    COMMAND_REJECTED = "command_rejected"    # refused; data["reason"] names the failed check
    FAILSAFE_HOLD = "failsafe_hold"          # no fresh valid command: the substation holds its state
    FALLBACK_SCHEDULE = "fallback_schedule"  # B only: the pre-signed rotation table took over
    # island power
    SUPPLY_CHANGED = "supply_changed"        # available supply G(t) changed
    FEEDER_SWITCHED = "feeder_switched"      # a feeder was opened or closed
    PROTECTIVE_SHED = "protective_shed"      # local protective load shedding (overload)
    UFLS_TRIP = "ufls_trip"                  # under-frequency load shedding tripped feeders
    ISLAND_COLLAPSE = "island_collapse"      # the island blacked out
    # ground truth only
    FAULT_ACTION = "fault_action"            # what an injected fault did (equivocate, crash, replay, ...)
    NET_LOSS = "net_loss"                    # a message was lost on every attempt


GENESIS = "0" * 64


@dataclass
class Event:
    seq: int
    t: float
    kind: EventKind
    actor: str                  # "validator:v3", "centre", "substation:S2", "sim"
    data: dict
    visibility: str = "public"  # "public" | "truth"
    prev_hash: str = GENESIS
    hash: str = ""

    def header(self) -> dict:
        return {
            "seq": self.seq, "t": round(self.t, 6), "kind": self.kind.value,
            "actor": self.actor, "data": self.data, "visibility": self.visibility,
            "prev_hash": self.prev_hash,
        }

    def to_dict(self) -> dict:
        d = self.header()
        d["hash"] = self.hash
        return d


@dataclass
class EventLog:
    events: List[Event] = field(default_factory=list)
    run_meta: dict = field(default_factory=dict)

    def append(self, t: float, kind: EventKind, actor: str, data: dict,
               visibility: str = "public") -> Event:
        if visibility not in ("public", "truth"):
            raise ValueError(f"visibility must be 'public' or 'truth', got {visibility!r}")
        prev = self.events[-1].hash if self.events else GENESIS
        ev = Event(len(self.events), t, kind, actor, data, visibility, prev)
        ev.hash = canonical_hash(ev.header())
        self.events.append(ev)
        return ev

    # ---- queries ----
    def of_kind(self, *kinds: EventKind, visibility: Optional[str] = None) -> Iterator[Event]:
        for e in self.events:
            if e.kind in kinds and (visibility is None or e.visibility == visibility):
                yield e

    def public(self) -> Iterator[Event]:
        return (e for e in self.events if e.visibility == "public")

    @property
    def head(self) -> str:
        return self.events[-1].hash if self.events else GENESIS

    def verify_chain(self) -> bool:
        prev = GENESIS
        for e in self.events:
            if e.prev_hash != prev or canonical_hash(e.header()) != e.hash:
                return False
            prev = e.hash
        return True

    # ---- export ----
    def write_jsonl(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_meta": self.run_meta}, ensure_ascii=False) + "\n")
            for e in self.events:
                fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    @staticmethod
    def read_jsonl(path: str) -> "EventLog":
        log = EventLog()
        with open(path, encoding="utf-8") as fh:
            first = json.loads(fh.readline())
            log.run_meta = first.get("run_meta", {})
            for line in fh:
                d = json.loads(line)
                log.events.append(Event(d["seq"], d["t"], EventKind(d["kind"]), d["actor"],
                                        d["data"], d["visibility"], d["prev_hash"], d["hash"]))
        return log
