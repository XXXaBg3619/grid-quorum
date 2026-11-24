"""Run one configuration over many runs, paired with its honest baseline (to be written next).

Pattern from evsim/run.py (BlokcChin@796b43c):
  * separate RNG streams for loads, network and faults, keyed by the seed, so
    the honest and the faulty run of a pair see identical loads and network
    (common random numbers) and the variance of the difference stays small;
  * run_paired(cfg, fault) runs both and returns per-run metrics, the paired
    difference (metrics.paired_diff) and 95% CIs over seeds (metrics.mean_ci95);
  * the same pairing across architectures: A, B, C and D get the same loads,
    supply scenario, network and fault, so only the decision path differs.
"""
