"""Simulation parameters (layout ported from evsim/config.py, BlokcChin@796b43c).

One frozen dataclass: with_() returns a modified copy and to_dict() goes into
every result's meta.json. Derived values are properties, so they cannot drift
from the parameters they come from.

Committee (C, D): standard BFT. f counts hacked and unreachable validators
together, and f and the quorum Q follow from n:

    f = (n - 1) // 3        Q = floor(2n / 3) + 1   (more than two thirds, CometBFT's rule)

For n = 3f + 1 this is the textbook Q = 2f + 1: n = 4 gives f = 1, Q = 3;
n = 7 gives f = 2, Q = 5 (the wartime setting). Liveness assumes partial
synchrony: round timeouts grow every round, so once the network behaves some
round is long enough to finish.

The power-model fields are placeholders until power.py is written; their names
follow the design doc (G(t), cold-load multiplier, UFLS).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

ARCHITECTURES = ("A", "B", "C", "D")
LOSS_MODELS = ("iid", "outage")


@dataclass(frozen=True)
class GridConfig:
    # --- Architecture under test ---
    arch: str = "D"                  # A central+backup | B central+local enforcer | C Taipower BFT | D multi-org BFT

    # --- Committee (C, D) ---
    n_validators: int = 7
    round_timeout_s: float = 3.0     # propose timeout in round 0 (CometBFT's timeout_propose)
    vote_timeout_s: float = 1.0      # prevote and precommit timeouts in round 0 (CometBFT's defaults)
    timeout_delta_s: float = 0.5     # added to every timeout per round (partial synchrony)

    # --- Network: every link between two nodes (see network.py) ---
    net_delay_s: float = 0.05        # base one-way delay
    net_jitter_s: float = 0.05       # extra delay, uniform in [0, jitter], per message
    link_loss_p: float = 0.0         # iid: per attempt; outage: share of time a node's link is down
    link_loss_model: str = "iid"     # "iid" | "outage" (Gilbert-Elliott on/off per node, evsim's B1)
    link_outage_mean_s: float = 600.0    # outage model: mean length of one outage
    link_retries: int = 0            # extra sends of a message by its sender
    link_retry_gap_s: float = 1.0    # time between sends

    # --- Dispatch and commands ---
    dispatch_period_s: float = 300.0     # a new allocation decision every 5 minutes
    horizon_h: float = 24.0          # length of one island event
    chain_id: str = "island-1"
    epoch: int = 1                   # the validator set is fixed (epoch changes are future work)
    command_ttl_s: float = 900.0     # secondary freshness check; sequence numbers and state versions come first

    # --- Power model (placeholders until power.py) ---
    cold_load_multiplier: float = 2.0    # a re-energised feeder draws this x its normal load ...
    cold_load_decay_s: float = 1800.0    # ... decaying back over this time
    ufls_margin: float = 0.05        # served load above G(t) x (1 + margin) trips UFLS

    # --- Run ---
    runs: int = 30                   # island events per seed
    seed: int = 0

    def __post_init__(self):
        if self.arch not in ARCHITECTURES:
            raise ValueError(f"arch must be one of {ARCHITECTURES}, got {self.arch!r}")
        if self.arch in ("C", "D") and self.n_validators < 4:
            raise ValueError("BFT needs at least 4 validators (f >= 1)")
        if self.link_loss_model not in LOSS_MODELS:
            raise ValueError(f"link_loss_model must be one of {LOSS_MODELS}, got {self.link_loss_model!r}")
        if not 0.0 <= self.link_loss_p <= 1.0:
            raise ValueError("link_loss_p must be in [0, 1]")

    # ---- derived ----
    @property
    def f(self) -> int:
        """Faults tolerated: hacked plus unreachable validators together."""
        return (self.n_validators - 1) // 3

    @property
    def quorum(self) -> int:
        """Q: signatures needed to commit, more than two thirds of n."""
        return 2 * self.n_validators // 3 + 1

    @property
    def horizon_s(self) -> float:
        return self.horizon_h * 3600.0

    @property
    def n_dispatches(self) -> int:
        return int(self.horizon_s // self.dispatch_period_s)

    def max_hop_s(self) -> float:
        """Slowest one-way message between two nodes, without loss or partitions."""
        return self.net_delay_s + self.net_jitter_s

    def round_timeout(self, rnd: int) -> float:
        """Propose timeout of round `rnd` (0-based); grows linearly, as in Tendermint."""
        return self.round_timeout_s + rnd * self.timeout_delta_s

    def vote_timeout(self, rnd: int) -> float:
        """Prevote and precommit timeouts of round `rnd`; grow like round_timeout()."""
        return self.vote_timeout_s + rnd * self.timeout_delta_s

    def with_(self, **kw) -> "GridConfig":
        return replace(self, **kw)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(f=self.f, quorum=self.quorum)
        return d
