"""Experiment 4 (docs/DESIGN.md section 5): replaying executed commands at the last hop.

One island in architecture D (C behaves the same here: a replay does not depend
on who runs the validators): 7 validators, 4 substations with 3 feeders each,
one group of feeders rotated off every 5 minutes for 6 hours
(lasthop.RotatingDispatch), no message loss. An attacker on the last hop
records every delivery (block plus commit proof, which it cannot forge) and
sends it again (faults.Replay):

  duplicate  each delivery once more, 5 s later
  reverse    after a feeder is switched, the delivery that last set it the
             other way, 10 s later (question 25: "cut feeder 3" again after
             power came back)
  late       as reverse, but only deliveries at least 30 minutes old

Every attack runs against substation verifiers that keep the signature check
(commit proof) and add one group of checks, and against the full verifier
with a right, a slow and a fast clock. The reference is the same seed and run
with the full verifier and no attack; feeder time that differs from it is the
damage. Checks pass when:

  * no verifier with the order or the version check executes a replayed command;
  * the full verifier with a right or slow clock (with or without the TTL)
    executes every committed command and its feeders never leave the reference;
  * the signature-only verifier does execute replays (otherwise the attack did
    nothing and the zeros above would prove nothing).

Writes <out>.csv (one row per verifier and attack), <out>.meta.json and
<out>.svg, and exits with status 1 if a check fails.

  python3 experiments/exp4_replay.py [--quick] [--seeds 3] [--runs 2] [--workers 4] [--out results/main/exp4_replay]
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import common
from gridq.config import GridConfig
from gridq.faults import Replay
from gridq.lasthop import default_feeders, run_lasthop
from gridq.metrics import (commands_executed, feeder_timelines, injected_outcomes, mean_ci95,
                           state_deviation_s)
from gridq.svgchart import matrix
from gridq.verifier import ALL_CHECKS

FULL = tuple(sorted(ALL_CHECKS))
NO_TTL = tuple(c for c in FULL if c != "ttl")
HOUR = 3600.0
VARIANTS = (                    # key, label, checks besides the signature, clock offset (s)
    ("sig_only", "只數簽名", (), 0.0),
    ("ttl", "＋有效期限", ("ttl",), 0.0),
    ("ttl_slow", "＋有效期限，時鐘慢 1 小時", ("ttl",), -HOUR),
    ("order", "＋高度與序號", ("order",), 0.0),
    ("version", "＋狀態版本", ("version",), 0.0),
    ("full", "全部檢查", FULL, 0.0),
    ("full_no_ttl", "全部檢查，不看有效期限", NO_TTL, 0.0),
    ("full_slow", "全部檢查，時鐘慢 1 小時", FULL, -HOUR),
    ("full_fast", "全部檢查，時鐘快 1 小時", FULL, HOUR),
)
ATTACKS = (("none", "不攻擊"), ("duplicate", "5 秒後重送同一則"),
           ("reverse", "切換後重送反向的舊指令"), ("late", "重送 30 分鐘以上的舊指令"))
REASONS = ("stale_seq", "state_mismatch", "expired")
MUST_HOLD = ("full", "full_no_ttl", "full_slow")       # every committed command executed, no deviation


def job(args):
    """Every verifier and attack for one seed and run: counts only."""
    seed, run, heights = args
    cfg = GridConfig(seed=seed)
    ref = run_lasthop(cfg, run=run, heights=heights)
    end = ref.cfg.horizon_s
    ref_tl = feeder_timelines(ref.log, ref.initial_states())
    out = []
    for key, _, checks, offset in VARIANTS:
        for attack, _ in ATTACKS:
            if key == "full" and attack == "none":
                hop = ref
            else:
                hop = run_lasthop(cfg, run=run, heights=heights, checks=checks, clock_offset_s=offset,
                                  fault=None if attack == "none" else Replay(attack))
            inj = injected_outcomes(hop.log)
            issued, executed = commands_executed(hop.log)
            dev = state_deviation_s(feeder_timelines(hop.log, hop.initial_states()), ref_tl, end)
            out.append(dict(variant=key, attack=attack, issued=issued, executed=executed,
                            unsafe_on_s=dev["unsafe_on"], wrong_off_s=dev["wrong_off"],
                            **{f"inj_{k}": v for k, v in inj.items()}))
    return out


def summarize(results):
    cells = defaultdict(list)
    for res in results:
        for c in res:
            cells[(c["variant"], c["attack"])].append(c)
    rows = []
    for key, label, checks, offset in VARIANTS:
        for attack, attack_label in ATTACKS:
            cs = cells[(key, attack)]
            tot = lambda k: sum(c.get(k, 0) for c in cs)
            on_m, on_ci = mean_ci95([c["unsafe_on_s"] / 60.0 for c in cs])
            off_m, off_ci = mean_ci95([c["wrong_off_s"] / 60.0 for c in cs])
            cmds, acc = tot("inj_commands"), tot("inj_accepted")
            share = acc / cmds if cmds else 0.0
            exec_share = tot("executed") / tot("issued") if tot("issued") else 0.0
            ok = True
            if "order" in checks or "version" in checks:
                ok = acc == 0
            if key in MUST_HOLD:
                ok = ok and tot("executed") == tot("issued") and tot("unsafe_on_s") == 0 and tot("wrong_off_s") == 0
            if key == "sig_only" and attack in ("reverse", "late"):
                ok = acc > 0
            row = dict(variant=key, variant_label=label, checks="+".join(checks) or "signature only",
                       clock_offset_s=offset, attack=attack, attack_label=attack_label, runs=len(cs),
                       replay_deliveries=tot("inj_deliveries"), replay_commands=cmds, replay_accepted=acc,
                       replay_accept_share=round(share, 4), replay_switches=tot("inj_switched"))
            for r in REASONS:
                row[f"rejected_{r}"] = tot(f"inj_rejected:{r}")
            row.update(ignored_duplicate=tot("inj_ignored"),
                       committed_commands=tot("issued"), executed_commands=tot("executed"),
                       executed_share=round(exec_share, 4),
                       unsafe_on_min_per_run=round(on_m, 2), unsafe_on_ci95=round(on_ci, 2),
                       wrong_off_min_per_run=round(off_m, 2), wrong_off_ci95=round(off_ci, 2),
                       passed=ok)
            rows.append(row)
    return rows


def svg(rows, runs, heights):
    by = {(r["variant"], r["attack"]): r for r in rows}
    shown = [a for a in ATTACKS if a[0] != "none"]
    labels = [v[1] for v in VARIANTS]
    values, tips = [], []
    for key, label, _, _ in VARIANTS:
        cells = [by[(key, a)] for a, _ in shown]
        values.append([c["replay_accept_share"] if c["replay_commands"] else None for c in cells])
        tips.append([f"{label}，{al}：重放的 {c['replay_commands']} 道指令執行了 {c['replay_accepted']} 道，"
                     f"其中 {c['replay_switches']} 道真的切換了饋線" if c["replay_commands"] else
                     f"{label}，{al}：沒有觸發任何重放（饋線沒有切換過）"
                     for c, (_, al) in zip(cells, shown)])
    minutes = lambda r: f"{r['unsafe_on_min_per_run'] + r['wrong_off_min_per_run']:,.0f} 分鐘"
    text_cols = [("不攻擊時\n合法指令執行率", [f"{by[(k, 'none')]['executed_share'] * 100:.0f}%" for k, *_ in VARIANTS]),
                 ("饋線偏離參考\n（反向重送）", [minutes(by[(k, 'reverse')]) for k, *_ in VARIANTS]),
                 ("饋線偏離參考\n（30 分鐘以上）", [minutes(by[(k, 'late')]) for k, *_ in VARIANTS])]
    cols = ["5 秒後\n重送同一則", "切換後重送\n反向的舊指令", "重送 30 分鐘\n以上的舊指令"]
    hours = heights * GridConfig().dispatch_period_s / HOUR
    return matrix(labels, cols, values, tips=tips, text_cols=text_cols, breaks=[4],
                  title="exp4 舊指令重放：變電所的哪一項檢查擋得住",
                  subtitle=(f"7 個驗證者、4 座變電所、每 5 分鐘輪停一組饋線；{runs} 次、每次 {hours:g} 小時，無訊息遺失\n"
                            "每一列都有 commit proof 簽章檢查；偏離是饋線狀態和「全部檢查、不攻擊」不同的時間（12 條饋線合計、每次平均）"),
                  heat_header="重放的指令被執行的比例", legend_label="重放的指令被執行的比例",
                  desc="各種變電所驗證規則面對三種重放攻擊時，重放指令被執行的比例，以及不攻擊時合法指令的執行率")


def main():
    ap = common.common_args(seeds=3, runs=2)
    ap.add_argument("--heights", type=int, default=72, help="dispatch slots per run (5 minutes each)")
    a = ap.parse_args()
    seeds, runs = (1, 1) if a.quick else (a.seeds, a.runs)
    heights = 24 if a.quick else a.heights
    out = a.out or os.path.join(common.ROOT, "results", "main", "exp4_replay")
    jobs = [(seed, run, heights) for seed in range(seeds) for run in range(runs)]
    rows = summarize(common.run_jobs(job, jobs, a.workers))

    print(f"{'verifier':<13} {'attack':<10} {'replayed':>8} {'executed':>8} {'switched':>8} "
          f"{'legit':>6} {'unsafe on':>9} {'wrong off':>9}")
    for r in rows:
        print(f"{r['variant']:<13} {r['attack']:<10} {r['replay_commands']:>8} {r['replay_accepted']:>8} "
              f"{r['replay_switches']:>8} {r['executed_share']:>6.0%} {r['unsafe_on_min_per_run']:>9.1f} "
              f"{r['wrong_off_min_per_run']:>9.1f}{'' if r['passed'] else '   FAIL'}")
    passed = all(r["passed"] for r in rows)
    meta = {
        "experiment": "exp4_replay", "step": "docs/DESIGN.md section 7, step 2",
        "config": GridConfig().to_dict(), "seeds": seeds, "runs_per_seed": runs, "heights_per_run": heights,
        "island": {"substations": default_feeders(), "rotation_groups": 3},
        "variants": {k: {"label": lab, "checks": list(c), "clock_offset_s": o} for k, lab, c, o in VARIANTS},
        "attacks": {k: lab for k, lab in ATTACKS},
        "pass_rule": "verifiers with the order or the version check execute no replayed command; the full "
                     "verifier with a right or slow clock, with or without the TTL, executes every committed "
                     "command and never deviates from the reference; the signature-only verifier does execute "
                     "replays of reversed and old commands",
        "all_passed": passed,
        "known_issues": [
            "C and D are the same here: the attack does not depend on who runs the validators. A and B's "
            "centre-signed commands go through the same checks (tests/test_verifier.py), not this experiment.",
            "no message loss: every honest copy arrives, so a committed command can fail only by a check; with "
            "loss a stale state report can also hold a command back for one slot (tests/test_lasthop.py)",
            "RotatingDispatch stands in for policy.py; the commands follow the substations' state reports on the "
            "chain, so a replay that flips a feeder is undone at the next dispatch slot at the latest",
            "an attacker who also withholds deliveries and releases them late is not in this experiment: only "
            "the TTL stops that, and only with a right clock",
            "a replay of the newest block is dropped as a copy of the trusted header, without an event "
            "(column ignored_duplicate)",
        ],
    }
    common.write_results(out, rows, meta)
    with open(out + ".svg", "w", encoding="utf-8") as fh:
        fh.write(svg(rows, seeds * runs, heights))
    print("wrote", out + ".svg")
    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
