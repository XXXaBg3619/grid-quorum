"""Step 1 of docs/DESIGN.md section 7: check the message-level consensus against theory.

Honest validators, every message lost independently with probability p, no
resends. For each committee size n and loss p the script runs heights one slot
apart and checks:

  1. round 0 against theory: the share of heights that commit in round 0
     matches theory.round_success_prob(n, Q, p); and at every single height,
     round 0 commits exactly when theory.round_outcome says it should, given
     which of that height's messages the network lost (same draws);
  2. safety and liveness: no height ever has two committed blocks, and every
     height that started is committed by every validator.

Writes <out>.csv (one row per n and p), <out>.meta.json and <out>.svg, and
exits with status 1 if a check fails.

  python3 experiments/check_consensus.py [--quick] [--workers 4] [--out results/main/check_consensus]
"""
from __future__ import annotations

import math
import os
import sys

import common
from gridq.config import GridConfig
from gridq.consensus import run_committee
from gridq.svgchart import chart
from gridq.theory import round_outcome, round_success_prob

NS = (4, 7)
PS = (0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3)
SLOT_S = 3600.0          # one height per hour, so heights rarely overlap even at p = 0.3;
                         # an overlapped height is left out of the round 0 check (clean_heights)
Z_MAX = 4.0              # allowed distance from theory, in standard errors ...
TOL = 0.005              # ... plus the Monte Carlo error of round_success_prob itself


def job(args):
    """All runs of one (n, p, seed): counts only, so the result is small to send back."""
    n, p, seed, runs, heights = args
    cfg = GridConfig(n_validators=n, link_loss_p=p, seed=seed, dispatch_period_s=SLOT_S)
    out = dict(n=n, p=p, heights=0, clean=0, round0=0, mismatch=0, conflicts=0, undecided=0,
               rounds=0, latency=[])
    for run in range(runs):
        com = run_committee(cfg, run=run, heights=heights, drain_s=SLOT_S)
        names, first = com.names, com.first_commits()
        out["conflicts"] += len(com.conflicting_heights())
        out["undecided"] += len(com.undecided_heights())
        out["heights"] += len(com.starts)
        for h, starts in com.starts.items():
            if h in first:
                out["rounds"] += first[h]["round"]
                out["latency"].append(round(first[h]["latency"], 4))
            if len(starts) < n or max(starts.values()) - min(starts.values()) > 1e-9:
                continue                                  # round 0 did not start everywhere at once
            out["clean"] += 1
            ok = h in first and first[h]["round"] == 0
            out["round0"] += ok
            got = lambda kind, i, j: com.net.deliver(f"{kind}:{h}:0:{names[i]}", names[i], names[j], 0.0) is not None
            out["mismatch"] += ok != round_outcome(n, com.q, names.index(com.proposer(h, 0)), got)
    return out


def quantile(xs, q):
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def summarize(results):
    rows = []
    for n in NS:
        for p in PS:
            rs = [r for r in results if (r["n"], r["p"]) == (n, p)]
            tot = {k: sum(r[k] for r in rs) for k in ("heights", "clean", "round0", "mismatch", "conflicts",
                                                     "undecided", "rounds")}
            q = 2 * n // 3 + 1
            th = round_success_prob(n, q, p)
            share = tot["round0"] / tot["clean"]
            se = math.sqrt(th * (1 - th) / tot["clean"])
            lat = sorted(x for r in rs for x in r["latency"])
            ok = (abs(share - th) <= Z_MAX * se + TOL and tot["mismatch"] == 0
                  and tot["conflicts"] == 0 and tot["undecided"] == 0)
            rows.append(dict(
                n=n, quorum=q, p=p, heights=tot["heights"], clean_heights=tot["clean"],
                round0_commits=tot["round0"], round0_share=round(share, 4),
                ci95=round(1.96 * math.sqrt(share * (1 - share) / tot["clean"]), 4),
                theory=round(th, 4), z=round((share - th) / se, 2) if se else 0.0,
                same_draw_mismatches=tot["mismatch"], conflicting_heights=tot["conflicts"],
                undecided_heights=tot["undecided"], mean_commit_round=round(tot["rounds"] / max(1, len(lat)), 3),
                latency_p50_s=quantile(lat, 0.5), latency_p95_s=quantile(lat, 0.95),
                latency_max_s=lat[-1] if lat else float("nan"), passed=ok))
    return rows


def svg(rows, heights_each):
    pct = lambda v: f"{v * 100:.0f}%"
    series, legend = [], []
    for slot, n in ((1, 4), (2, 7)):
        rs = [r for r in rows if r["n"] == n]
        q = rs[0]["quorum"]
        series.append({"label": f"n = {n}", "slot": slot, "end_label": f"n = {n}, Q = {q}",
                       "points": [(r["p"], r["round0_share"],
                                   f"n = {n}, 遺失 {pct(r['p'])}：模擬 {r['round0_share']:.3f} ± {r['ci95']:.3f}，"
                                   f"理論 {r['theory']:.3f}（{r['clean_heights']} 個高度）") for r in rs],
                       "band": [(r["p"], max(0.0, r["round0_share"] - r["ci95"]), min(1.0, r["round0_share"] + r["ci95"]))
                                for r in rs]})
        legend.append((f"模擬 n = {n}（Q = {q}）", slot, "line"))
    refs = [{"label": f"理論 n = {n}", "points": [(r["p"], r["theory"]) for r in rows if r["n"] == n],
             "label_at": "end"} for n in NS]
    legend.append(("理論值 round_success_prob", 0, "ref"))
    return chart([{"title": "", "series": series, "refs": refs}],
                 title="第 0 輪就提交的高度比例：逐節點共識 vs 理論",
                 subtitle=f"誠實驗證者，每則訊息各自以機率 p 遺失、不重送；每點約 {heights_each} 個高度，帶狀為 95% 信賴區間",
                 legend=legend, x_label="訊息遺失率 p", y_label="第 0 輪提交比例",
                 x_domain=(0.0, 0.3), x_ticks=list(PS[::2]) + [PS[-1]], x_fmt=pct, y_domain=(0.0, 1.0), y_fmt=pct,
                 panel_w=560, desc="逐節點共識在 iid 訊息遺失下第 0 輪的提交比例，與 theory.round_success_prob 比較")


def main():
    ap = common.common_args(seeds=10, runs=4)
    ap.add_argument("--heights", type=int, default=50, help="heights per run")
    a = ap.parse_args()
    seeds, runs = common.sizes(a)
    heights = 20 if a.quick else a.heights
    out = a.out or os.path.join(common.ROOT, "results", "main", "check_consensus")
    jobs = [(n, p, seed, runs, heights) for n in NS for p in PS for seed in range(seeds)]
    rows = summarize(common.run_jobs(job, jobs, a.workers))

    print(f"{'n':>2} {'p':>5} {'heights':>7} {'round 0':>8} {'theory':>7} {'z':>6} {'mismatch':>8} {'conflict':>8} {'undecided':>9}")
    for r in rows:
        print(f"{r['n']:>2} {r['p']:>5} {r['clean_heights']:>7} {r['round0_share']:>8.3f} {r['theory']:>7.3f} {r['z']:>6.2f}"
              f" {r['same_draw_mismatches']:>8} {r['conflicting_heights']:>8} {r['undecided_heights']:>9}"
              f"{'' if r['passed'] else '   FAIL'}")
    passed = all(r["passed"] for r in rows)
    meta = {
        "experiment": "check_consensus", "step": "docs/DESIGN.md section 7, step 1",
        "config": GridConfig(dispatch_period_s=SLOT_S).to_dict(), "n_values": NS, "loss_values": PS,
        "seeds": seeds, "runs_per_seed": runs, "heights_per_run": heights,
        "pass_rule": f"|share - theory| <= {Z_MAX} standard errors + {TOL}; no same-draw mismatch, "
                     "no conflicting height, no undecided height",
        "all_passed": passed,
        "known_issues": [
            "round_success_prob is itself a Monte Carlo estimate (20000 trials, error about 0.003)",
            "only round 0 is compared with theory: later rounds start with locks and valid values",
            "clean_heights leaves out heights whose round 0 did not start at the same moment everywhere",
            "every cell reuses the same network draws (common random numbers keyed by seed, run and message), "
            "so the cells' deviations from theory move together instead of scattering independently",
        ],
    }
    common.write_results(out, rows, meta)
    with open(out + ".svg", "w", encoding="utf-8") as fh:
        fh.write(svg(rows, seeds * runs * heights))
    print("wrote", out + ".svg")
    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
