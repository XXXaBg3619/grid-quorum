"""Experiment 5 (docs/DESIGN.md section 5): commands that are each legal but harmful together.

The default island event (24 hours, a strike near hour 3, 45 % of the peak
after it). Ten minutes after the strike an attacker takes over the decision
for the rest of the day and sends, instead of the policy's targets
(faults.attack_targets):

  restore_all   every feeder on: each command is one a dispatcher may send,
                together they draw far more than the island has
  cut_critical  the policy's targets but every critical feeder off: an obvious
                violation of the rules
  starve        the policy's targets but substation S4's non-critical feeders
                off all day: each command is legal, the rotation is not fair

Who the attacker has to control:
  A, B    the dispatch centre (faults.HackedCentre): one node. B's substations
          check each command locally (no critical feeder off, at most
          cfg.local_max_switch switchings per period), A's do not.
  C, D    b validators' keys (faults.CompromisedValidators), b = 1, 2 (within
          f), 4 (more than f, fewer than Q) and 5 (Q): their proposals carry
          the attack commands, and at every slot they forge a commit proof with
          the keys they hold and send their own block to the substations.

Checks pass when:
  * restore_all: B's local checks let the joint overload through, so B, like A,
    trips UFLS after the strike in every run;
  * cut_critical: A executes the cuts (critical feeders lose power in every
    run); B refuses them (critical feeders always on);
  * starve: A and B starve S4 (its non-critical feeders get less power than
    without the attack, in every run);
  * C, D with b <= 4: no attack command is executed, no attack block is
    committed, and the committee decides exactly as without the attack;
  * C, D with b = 5: the forged blocks are executed (the attack works: Q keys
    are the whole trust).

Writes <out>.csv, <out>.meta.json and <out>.svg and exits with status 1 if a
check fails.

  python3 experiments/exp5_joint_overload.py [--quick] [--seeds 5] [--runs 4] [--workers 4]
      [--out results/main/exp5_joint_overload] [--shard I/N | --merge N]
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import common
from gridq.config import GridConfig
from gridq.events import EventKind
from gridq.faults import CompromisedValidators, HackedCentre
from gridq.hashing import canonical_hash
from gridq.loads import Loads, default_island
from gridq.metrics import after_event, feeder_timelines, island_metrics, mean_ci95, on_intervals
from gridq.power import supply_scenario
from gridq.sim import Simulator
from gridq.svgchart import matrix

V = [f"v{i}" for i in range(7)]
START_AFTER_S = 600.0
MODES = (("restore_all", "全部復電"), ("cut_critical", "切掉關鍵饋線"), ("starve", "讓 S4 一直停電"))
ATTACKERS = (   # key, architecture, label, compromised validators (C) or None (hacked centre)
    ("A", "A", "A：駭入調度中心（1 個節點）", None),
    ("B", "B", "B：駭入調度中心（1 個節點）", None),
    ("C1", "C", "C、D：1 個驗證者", 1),
    ("C2", "C", "C、D：2 個驗證者（f）", 2),
    ("C4", "C", "C、D：4 個驗證者（超過 f、少於 Q）", 4),
    ("C5", "C", "C、D：5 個驗證者（Q）", 5),
)
STARVED = "S4"


def decisions_hash(log):
    return canonical_hash([[e.data["expires"], e.data["substation"], e.data["feeder"], e.data["target"]]
                           for e in log.of_kind(EventKind.COMMAND_ISSUED)])


def starved_share(log, specs, end):
    """Share of the day S4's non-critical feeders were on."""
    initial = {}
    for sp in specs:
        initial.setdefault(sp.substation, {})[sp.name] = "on"
    tl = feeder_timelines(log, initial)
    fs = [sp for sp in specs if sp.substation == STARVED and not sp.critical]
    return sum(b - a for sp in fs for a, b in on_intervals(tl[(sp.substation, sp.name)], end)) / (len(fs) * end)


def job(args):
    seed, run = args
    base = GridConfig(seed=seed)
    specs = default_island()
    supply = supply_scenario(base, Loads(base, specs, run).peak_kw(), run)
    strike = supply.strike()
    start = strike + START_AFTER_S
    out = []

    def one(arch, mode, key, fault):
        cfg = base.with_(arch=arch)
        sim = Simulator(cfg, fault, run, specs=specs, supply=supply).run()
        log = sim.log
        m = island_metrics(log)
        m.update(after_event(log, cfg, strike, len(sim.link.subs)))
        accepted = [e for e in log.of_kind(EventKind.COMMAND_ACCEPTED) if e.t >= start]
        rejected = [e for e in log.of_kind(EventKind.COMMAND_REJECTED) if e.t >= start]
        row = dict(mode=mode, attacker=key, arch=arch, seed=seed, run=run, decisions=decisions_hash(log),
                   starved_on_share=round(starved_share(log, specs, cfg.horizon_s), 6),
                   refused_by_rule=sum(1 for e in rejected if e.data.get("reason") == "policy_violation"),
                   refused_proof=sum(1 for e in rejected if e.data.get("reason") == "insufficient_quorum"))
        if isinstance(fault, HackedCentre):
            row.update(attack_sent=fault.sent, attack_executed=len(accepted))
        elif isinstance(fault, CompromisedValidators):
            com = sim.link.com
            committed = {c["block"] for d in com.decisions.values() for c in d.values()}
            row.update(attack_sent=sum(n for _, _, n in fault.forged), attack_proposals=len(fault.proposed),
                       attack_blocks_committed=len(fault.proposed & committed),
                       attack_executed=sum(1 for e in accepted if str(e.data.get("delivery", "")).startswith("forge:")))
        out.append({**row, **{k: m[k] for k in ("ufls_events_after", "ufls_slots_after", "collapses", "dark_h",
                                                "critical_served_share", "served_share", "fairness", "availability")}})

    for arch in ("A", "B", "C"):
        one(arch, "none", arch if arch != "C" else "C0", None)
    for mode, _ in MODES:
        for key, arch, _, b in ATTACKERS:
            fault = (HackedCentre(mode, start=start) if b is None
                     else CompromisedValidators(V[7 - b:], mode, start=start))
            one(arch, mode, key, fault)
    return out


def summarize(results):
    cells = defaultdict(list)
    for res in results:
        for c in res:
            cells[(c["mode"], c["attacker"])].append(c)
    ref = {(c["arch"], c["seed"], c["run"]): c for k in ("A", "B", "C0") for c in cells[("none", k)]}
    rows = []
    for mode, mode_label in (("none", "沒有攻擊"),) + MODES:
        attackers = ((("A", "A", "A 中央＋備援", None), ("B", "B", "B 中央＋在地檢查", None),
                      ("C0", "C", "C、D 委員會", 0)) if mode == "none" else ATTACKERS)
        for key, arch, label, b in attackers:
            cs = cells[(mode, key)]
            base = lambda c: ref[(c["arch"], c["seed"], c["run"])]  # noqa: E731
            every = lambda pred: all(pred(c) for c in cs)  # noqa: E731
            ok = True
            if mode == "restore_all" and arch in "AB":
                ok = every(lambda c: c["ufls_events_after"] > 0)
            elif mode == "cut_critical" and arch == "A":
                ok = every(lambda c: c["critical_served_share"] < 1.0)
            elif mode == "cut_critical" and arch == "B":
                ok = every(lambda c: c["critical_served_share"] == 1.0 and c["refused_by_rule"] > 0)
            elif mode == "starve" and arch in "AB":
                ok = every(lambda c: c["starved_on_share"] < base(c)["starved_on_share"])
            if arch == "C" and mode != "none":
                if b <= 4:
                    ok = every(lambda c: c["attack_executed"] == 0 and c["attack_blocks_committed"] == 0
                               and c["decisions"] == base(c)["decisions"])
                else:
                    ok = every(lambda c: c["attack_executed"] > 0)
            mean = lambda k: mean_ci95([float(c[k]) for c in cs])  # noqa: E731
            row = dict(mode=mode, mode_label=mode_label, attacker=key, arch=arch, label=label,
                       compromised="" if b is None else b, runs=len(cs),
                       attack_sent=round(sum(c.get("attack_sent", 0) for c in cs) / len(cs), 1),
                       attack_executed=round(sum(c.get("attack_executed", 0) for c in cs) / len(cs), 1),
                       attack_proposals=round(sum(c.get("attack_proposals", 0) for c in cs) / len(cs), 1),
                       attack_blocks_committed=sum(c.get("attack_blocks_committed", 0) for c in cs),
                       refused_by_rule=round(sum(c["refused_by_rule"] for c in cs) / len(cs), 1),
                       refused_proof=round(sum(c["refused_proof"] for c in cs) / len(cs), 1),
                       same_decisions_as_honest=round(sum(c["decisions"] == base(c)["decisions"] for c in cs) / len(cs),
                                                      4) if arch == "C" else "")
            for k in ("ufls_events_after", "ufls_slots_after", "collapses", "dark_h", "critical_served_share",
                      "served_share", "fairness", "starved_on_share", "availability"):
                m, ci = mean(k)
                row[k] = round(m, 4)
                if k in ("ufls_events_after", "critical_served_share", "served_share", "starved_on_share"):
                    row[k + "_ci95"] = round(ci, 4)
            row["passed"] = ok
            rows.append(row)
    return rows


def svg(rows, runs):
    labels, values, tips, ufls, crit, s4, breaks = [], [], [], [], [], [], []
    for r in rows:
        labels.append(f"{r['mode_label']}｜{r['label']}")
        share = r["attack_executed"] / r["attack_sent"] if r["attack_sent"] else (None if r["mode"] == "none" else 0.0)
        values.append([share, r["ufls_slots_after"]])
        tips.append([(f"{r['mode_label']}，{r['label']}：平均送出 {r['attack_sent']:.0f} 道攻擊指令，"
                      f"變電所執行 {r['attack_executed']:.0f} 道；在地規則擋下 {r['refused_by_rule']:.0f} 道、"
                      f"簽名不足擋下 {r['refused_proof']:.0f} 道") if r["mode"] != "none" else "沒有攻擊",
                     f"{r['mode_label']}，{r['label']}：打擊後 {r['ufls_slots_after'] * 100:.1f}% 的時槽有低頻卸載，"
                     f"每次平均 {r['ufls_events_after']:.1f} 次"])
        ufls.append(f"{r['ufls_events_after']:.0f}")
        crit.append(f"{r['critical_served_share'] * 100:.1f}%")
        s4.append(f"{r['starved_on_share'] * 100:.0f}%")
        if r["attacker"] in ("C0", "C5"):
            breaks.append(len(labels) - 1)
    return matrix(labels, ["攻擊指令\n被執行的比例", "打擊後有低頻\n卸載的時槽"], values, tips=tips, breaks=breaks[:-1],
                  text_cols=[("打擊後低頻\n卸載次數", ufls), ("關鍵饋線\n供電時間", crit), ("S4 非關鍵饋線\n供電時間", s4)],
                  title="exp5 每道單看都合規、合起來有害：在地檢查擋得住明顯違規，擋不住組合",
                  subtitle=(f"12 條饋線、4 座變電所，24 小時、凌晨約 3 點打擊後剩 45% 供電；打擊後 10 分鐘起攻擊到當天結束；"
                            f"每列 {runs} 次\n"
                            "A、B：攻擊者控制調度中心（1 個節點，用中心自己的金鑰簽）\n"
                            "C、D：攻擊者握有幾個驗證者的金鑰，提案夾帶攻擊指令，並每個時槽自己簽一份提交證明直接送到變電所"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else ">99%" if 0.995 <= v < 1 else f"{v * 100:.0f}%",
                  heat_header="比例", legend_label="比例",
                  desc="三種攻擊下，各架構與不同被攻破節點數時，攻擊指令被執行的比例、低頻卸載、關鍵饋線與 S4 的供電時間")


def main():
    ap = common.common_args(seeds=5, runs=4)
    a = ap.parse_args()
    seeds, runs = (1, 2) if a.quick else (a.seeds, a.runs)
    out = a.out or os.path.join(common.ROOT, "results", "main", "exp5_joint_overload")
    jobs = [(seed, run) for seed in range(seeds) for run in range(runs)]
    results = common.run_grid(job, jobs, a, out)
    if results is None:
        return
    rows = summarize(results)
    print(f"{'mode':<12} {'attacker':<4} {'sent':>6} {'exec':>6} {'rule':>6} {'proof':>6} {'ufls>':>6} {'crit':>6} "
          f"{'served':>7} {'fair':>5} {'S4':>5}")
    for r in rows:
        print(f"{r['mode']:<12} {r['attacker']:<4} {r['attack_sent']:>6.0f} {r['attack_executed']:>6.0f} "
              f"{r['refused_by_rule']:>6.0f} {r['refused_proof']:>6.0f} {r['ufls_events_after']:>6.1f} "
              f"{r['critical_served_share']:>6.3f} {r['served_share']:>7.4f} {r['fairness']:>5.3f} "
              f"{r['starved_on_share']:>5.2f}{'' if r['passed'] else '   FAIL'}")
    passed = all(r["passed"] for r in rows)
    common.write_results(out, rows, {
        "experiment": "exp5_joint_overload", "step": "docs/DESIGN.md section 7, step 4", "seeds": seeds,
        "runs_per_seed": runs, "config": GridConfig().to_dict(), "attack_starts_after_strike_s": START_AFTER_S,
        "modes": dict(MODES), "starved_substation": STARVED,
        "attackers": {k: {"arch": arch, "label": lab, "compromised_validators": b} for k, arch, lab, b in ATTACKERS},
        "pass_rule": "restore_all: A and B trip UFLS after the strike in every run; cut_critical: A loses critical "
                     "load in every run, B never does and refuses commands; starve: A and B give S4 less power than "
                     "without the attack in every run; C, D with b <= 4: no attack command executed, no attack "
                     "block committed, decisions as without the attack; C, D with b = 5: forged commands executed",
        "all_passed": passed,
        "known_issues": [
            "C stands for D: the same code; how likely b compromised validators are is experiment 6",
            "the compromised validators otherwise run the protocol honestly; from f + 1 = 3 on they could also "
            "stall it by not voting (experiments 1 and 2)",
            "with Q keys the attacker's chain captures the substations: they refuse the committee's blocks from "
            "then on (stale_seq, header_mismatch), so recovering needs a new validator set (epoch change: future "
            "work), not just the attacker leaving",
            "B's local rule sees one substation: restoring every feeder stays within cfg.local_max_switch per "
            "period because most substations have few feeders off at a time; a rule that knew the island's "
            "supply would need the global state, which is what C and D bring",
        ]})
    with open(out + ".svg", "w", encoding="utf-8") as fh:
        fh.write(svg(rows, seeds * runs))
    print("wrote", out + ".svg")
    common.drop_shards(out, a)
    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
