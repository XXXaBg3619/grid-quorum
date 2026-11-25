"""Experiment 3 (docs/DESIGN.md section 5): partitions, delay and jamming.

Part consensus (run_committee, n = 7, f = 2, Q = 5; 36 five-minute slots):

  partitions  from slot 6 to 18 (one hour) the validators split into groups
              that cannot reach each other: 5|2 and 6|1 (one side keeps a
              quorum), 4|3 and 3|2|2 (no side has one), and 4|3 switching on
              and off every minute
  delay       every message takes d + Uniform(0, d), d from 0.5 to 4 s, all
              run long (the propose timeout starts at 3 s and grows by 0.5 s
              per round: partial synchrony)
  jamming     every validator's link is down a share p of the time, in
              outages of 10 minutes on average (network.py, outage model), all
              run long; theory.quorum_up_prob(7, 5, p) is the share of time at
              least Q validators are up

Checks pass when: no two different blocks are ever committed at one height;
5|2 and 6|1: every slot of the partition gets a block (the side with a quorum
goes on) and after it heals every validator holds the same complete chain;
4|3 and 3|2|2: nothing is committed during the partition and commits resume
after it; flapping 4|3 and every delay: every validator ends with the same
complete chain, and with every delay every slot gets a block.

Part island (the default island event, a strike near hour 3, 45 % of the peak
after it); from the strike, for two hours:

  split        A, B: {centre, S1, S2} | {backup, S3, S4}: each centre reaches
               half the island; C: {v0-v3, S1, S2} | {v4-v6, S3, S4}: no side
               has a quorum
  majority     A, B: {centre, backup, S1, S2} | {S3, S4}; C: {v0-v4, S1, S2} |
               {v5, v6, S3, S4}: the side with S1 and S2 can decide
  subs_cut     S3 and S4 cut off from every other node
and all day, jamming: every link down 10 % of the time (outages of 10 minutes).

Checks pass when: C never commits two blocks at one height or collapses the
island; split: C decides nothing during the partition, and A and B do run two
active centres at once (the split brain the design doc warns about, question
26); majority and subs_cut: C's S1 and S2 verify a fresh decision in every slot
while S3 and S4 hold (no switching by command); subs_cut: B's S3 and S4 start
their pre-signed tables while S1 and S2 follow the centre (half live, half
table: question 26).

Writes <out>.csv, .meta.json, .svg (consensus) and <out>_island.* and exits
with status 1 if a check fails.

  python3 experiments/exp3_partition.py [--part consensus|island|all] [--quick] [--seeds 5] [--runs 4]
      [--workers 4] [--out results/main/exp3_partition] [--shard I/N | --merge N]
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import common
from gridq.config import GridConfig
from gridq.consensus import run_committee
from gridq.events import EventKind
from gridq.loads import Loads, default_island
from gridq.metrics import after_event, island_metrics, mean_ci95, window_metrics
from gridq.power import supply_scenario
from gridq.sim import Simulator
from gridq.svgchart import matrix
from gridq.theory import quorum_up_prob

V = [f"v{i}" for i in range(7)]
HEIGHTS, W0, W1 = 36, 6, 18                      # consensus part: slots, partition [6, 18) = one hour
PERIOD = GridConfig().dispatch_period_s
FLAP_S = 60.0
CONSENSUS = (   # key, label, kind, parameter
    ("none", "沒有故障", "none", None),
    ("p52", "分成 5｜2", "partition", [V[:5], V[5:]]),
    ("p61", "分成 6｜1", "partition", [V[:6], V[6:]]),
    ("p43", "分成 4｜3", "partition", [V[:4], V[4:]]),
    ("p322", "分成 3｜2｜2", "partition", [V[:3], V[3:5], V[5:]]),
    ("flap43", "4｜3 每分鐘切換", "flap", [V[:4], V[4:]]),
    ("d0.5", "延遲 0.5～1 秒", "delay", 0.5),
    ("d1", "延遲 1～2 秒", "delay", 1.0),
    ("d2", "延遲 2～4 秒", "delay", 2.0),
    ("d4", "延遲 4～8 秒", "delay", 4.0),
    ("j05", "干擾 5%", "jam", 0.05),
    ("j10", "干擾 10%", "jam", 0.10),
    ("j20", "干擾 20%", "jam", 0.20),
)
ARCHES = (("A", "A 中央＋備援"), ("B", "B 中央＋在地檢查"), ("C", "C、D 委員會"))
SUBS = ["S1", "S2", "S3", "S4"]
ISLAND = (("none", "沒有故障"), ("split", "各連到一半的變電所"), ("majority", "一側能決策、另一半變電所斷線"),
          ("subs_cut", "S3、S4 和所有節點斷線"), ("jam", "全天干擾 10%"))
WINDOW_S = 7200.0


def island_groups(arch, key):
    if key == "split":
        return ([{"centre", "S1", "S2"}, {"backup", "S3", "S4"}] if arch in "AB"
                else [set(V[:4]) | {"S1", "S2"}, set(V[4:]) | {"S3", "S4"}])
    if key == "majority":
        return ([{"centre", "backup", "S1", "S2"}, {"S3", "S4"}] if arch in "AB"
                else [set(V[:5]) | {"S1", "S2"}, set(V[5:]) | {"S3", "S4"}])
    if key == "subs_cut":
        return [({"centre", "backup"} if arch in "AB" else set(V)) | {"S1", "S2"}]
    return None


def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


# ---------------------------------------------------------------- consensus part

def consensus_job(args):
    key, seed, runs = args
    _, _, kind, param = next(s for s in CONSENSUS if s[0] == key)
    cfg = GridConfig(seed=seed)
    t0, t1 = W0 * PERIOD, W1 * PERIOD
    parts = []
    if kind == "delay":
        cfg = cfg.with_(net_delay_s=param, net_jitter_s=param)
    elif kind == "jam":
        cfg = cfg.with_(link_loss_model="outage", link_loss_p=param)
    elif kind == "partition":
        parts = [(t0, t1, param)]
    elif kind == "flap":
        parts = [(t, t + FLAP_S, param) for t in [t0 + 2 * FLAP_S * i for i in range(int((t1 - t0) // (2 * FLAP_S)))]]
    window = kind in ("partition", "flap")
    out = defaultdict(int, key=key)
    out["latency"], out["resume"] = [], []
    for run in range(runs):
        com = run_committee(cfg, run=run, heights=HEIGHTS, drain_s=1800.0, partitions=parts)
        commits = [c for d in com.decisions.values() for c in d.values()]
        slots = {e["slot"] for v in com.names for e in com.nodes[v].chain}
        chains = {tuple(e["hash"] for e in com.nodes[v].chain) for v in com.names}
        span = range(W0, W1) if window else range(HEIGHTS)
        out["runs"] += 1
        out["slots"] += len(span)
        out["decided"] += len(slots & set(span))
        out["commits_in_window"] += sum(1 for c in commits if window and t0 <= c["t"] < t1)
        out["conflicts"] += len(com.conflicting_heights())
        out["undecided"] += len(com.undecided_heights())
        out["chain_mismatch"] += len(chains) > 1
        if kind == "partition" and key in ("p43", "p322"):
            after = [c["t"] for c in commits if c["t"] >= t1]
            out["resume"].append(round(min(after) - t1, 3) if after else None)
        for h, c in com.first_commits().items():
            if c["latency"] is not None and (not window or t0 <= min(com.starts[h].values()) < t1):
                out["latency"].append(round(c["latency"], 4))
                out["rounds"] += c["round"]
    return dict(out)


def consensus_rows(results):
    cells = defaultdict(list)
    for r in results:
        cells[r["key"]].append(r)
    rows = []
    for key, label, kind, param in CONSENSUS:
        rs = cells[key]
        tot = lambda k: sum(r.get(k, 0) for r in rs)  # noqa: E731
        lat = [x for r in rs for x in r["latency"]]
        resume = [x for r in rs for x in r["resume"]]
        share = tot("decided") / tot("slots")
        ok = tot("conflicts") == 0
        if key in ("p52", "p61"):
            ok = ok and share == 1.0 and tot("chain_mismatch") == 0 and tot("undecided") == 0
        elif key in ("p43", "p322"):
            ok = ok and tot("commits_in_window") == 0 and resume and None not in resume and tot("chain_mismatch") == 0
        elif kind in ("flap", "none"):
            ok = ok and tot("chain_mismatch") == 0 and tot("undecided") == 0
        elif kind == "delay":
            ok = ok and share == 1.0 and tot("chain_mismatch") == 0 and tot("undecided") == 0
        theory = quorum_up_prob(7, 5, param) if kind == "jam" else ""
        rows.append(dict(
            scenario=key, label=label, kind=kind, parameter="" if param is None or kind in ("partition", "flap")
            else param, groups="|".join(",".join(g) for g in param) if kind in ("partition", "flap") else "",
            runs=tot("runs"), slots=tot("slots"), decided_slots=tot("decided"), decided_share=round(share, 4),
            theory_quorum_up=round(theory, 4) if theory != "" else "",
            commits_in_window=tot("commits_in_window"),
            latency_p50_s=round(quantile(lat, 0.5), 3) if lat else "",
            latency_p95_s=round(quantile(lat, 0.95), 3) if lat else "",
            mean_round=round(tot("rounds") / len(lat), 3) if lat else "",
            resume_mean_s=round(sum(x for x in resume if x is not None) / len(resume), 2) if resume else "",
            conflicts=tot("conflicts"), undecided_heights=tot("undecided"), chain_mismatch_runs=tot("chain_mismatch"),
            passed=ok))
    return rows


def consensus_svg(rows, runs):
    labels, values, tips, lat, rounds, extra = [], [], [], [], [], []
    secs = lambda v: "—" if v == "" else (f"{v / 60:.0f} 分鐘" if v >= 600 else f"{v:.1f} 秒")  # noqa: E731
    for r in rows:
        labels.append(r["label"])
        values.append([r["decided_share"]])
        span = "分區那一小時" if r["kind"] in ("partition", "flap") else "全程"
        tips.append([f"{r['label']}：{span}的 {r['slots']} 個時槽中有 {r['decided_slots']} 個提交了區塊"])
        lat.append(secs(r["latency_p95_s"]))
        rounds.append("—" if r["mean_round"] == "" else f"{r['mean_round']:.2f}")
        if r["theory_quorum_up"] != "":
            extra.append(f"理論：至少 5 個連得上的時間 {r['theory_quorum_up'] * 100:.1f}%")
        elif r["resume_mean_s"] != "":
            extra.append(f"分區結束後 {secs(r['resume_mean_s'])} 恢復提交")
        else:
            extra.append("")
    return matrix(labels, ["有區塊的時槽"], values, tips=tips, breaks=[0, 5, 9],
                  text_cols=[("提交延遲 p95", lat), ("平均輪數", rounds), ("說明", extra)],
                  title="exp3 分區、延遲與干擾：安全不變，活性看有沒有 Q 個連得上",
                  subtitle=(f"7 個驗證者（Q = 5），36 個 5 分鐘時槽；分區在第 6 到 18 個時槽（一小時）；每列 {runs} 次\n"
                            "分區、切換：只看分區那一小時；延遲、干擾：全程。任何情況都沒有兩個區塊在同一高度被提交"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else ">99%" if 0.995 <= v < 1 else f"{v * 100:.0f}%",
                  heat_header="時槽的比例", legend_label="時槽的比例",
                  desc="各種分區、延遲和干擾下，有區塊提交的時槽比例、提交延遲、平均輪數，以及干擾時的理論值")


# ---------------------------------------------------------------- island part

def island_job(args):
    seed, run = args
    base = GridConfig(seed=seed)
    specs = default_island()
    supply = supply_scenario(base, Loads(base, specs, run).peak_kw(), run)
    strike = supply.strike()
    t1 = strike + WINDOW_S
    out = []
    for arch, _ in ARCHES:
        for key, _ in ISLAND:
            cfg = base.with_(arch=arch)
            if key == "jam":
                cfg = cfg.with_(link_loss_model="outage", link_loss_p=0.10)
            groups = island_groups(arch, key)
            parts = [(strike, t1, groups)] if groups else []
            sim = Simulator(cfg, None, run, specs=specs, supply=supply, partitions=parts).run()
            log = sim.log
            m = island_metrics(log)
            m.update(after_event(log, cfg, strike, len(sim.link.subs)))
            m.update(window_metrics(log, cfg, strike, t1 if groups else strike))
            k0, k1 = int(strike // PERIOD) + 1, (cfg.n_dispatches if key == "jam" else int(t1 // PERIOD))
            fresh = defaultdict(set)
            issuers = defaultdict(set)
            for e in log.of_kind(EventKind.DECISION_APPLIED):
                s, k = e.data.get("substation"), e.data.get("slot")
                if type(k) is int and k0 <= k < k1 and e.t < (k + 1) * PERIOD:
                    fresh[s].add(k)
                if type(k) is int and "issuer" in e.data:
                    issuers[k].add(e.data["issuer"])
            two = sum(1 for k, iss in issuers.items() if len(iss) > 1)
            dec = defaultdict(set)
            for e in log.of_kind(EventKind.DECISION):
                dec[e.data["slot"]].add(e.data.get("issuer", "committee"))
            m["fresh_in_window_s12"] = round(sum(len(fresh[s]) for s in ("S1", "S2")) / (2 * (k1 - k0)), 4)
            m["fresh_in_window_s34"] = round(sum(len(fresh[s]) for s in ("S3", "S4")) / (2 * (k1 - k0)), 4)
            m["command_switches_s34_in"] = sum(1 for e in log.of_kind(EventKind.FEEDER_SWITCHED)
                                               if e.data.get("cause") == "command" and strike <= e.t < t1
                                               and e.data.get("substation") in ("S3", "S4"))
            m["tables_started"] = sorted({e.data["substation"] for e in log.of_kind(EventKind.FALLBACK_SCHEDULE)
                                          if e.data.get("action") == "start"})
            m["two_centre_slots"] = sum(1 for k, iss in dec.items() if len(iss) > 1)
            m["two_issuer_applied_slots"] = two
            m["conflicts"] = len(sim.link.com.conflicting_heights()) if arch == "C" else 0
            out.append(dict(arch=arch, scenario=key, seed=seed, run=run, **m))
    return out


def island_rows(results):
    cells = defaultdict(list)
    for res in results:
        for c in res:
            cells[(c["arch"], c["scenario"])].append(c)
    rows = []
    for key, label in ISLAND:
        for arch, arch_label in ARCHES:
            cs = cells[(arch, key)]
            every = lambda pred: all(pred(c) for c in cs)  # noqa: E731
            ok = True
            if arch == "C":
                ok = every(lambda c: c["conflicts"] == 0 and c["collapses"] == 0)
                if key == "split":
                    ok = ok and every(lambda c: c["decisions_in"] == 0)
                if key in ("majority", "subs_cut"):
                    ok = ok and every(lambda c: c["fresh_in_window_s12"] == 1.0 and c["command_switches_s34_in"] == 0)
            elif key == "split":
                ok = every(lambda c: c["two_centre_slots"] > 0)
            if arch == "B" and key == "subs_cut":
                ok = ok and every(lambda c: c["tables_started"] == ["S3", "S4"] and c["fresh_in_window_s12"] == 1.0)
            mean = lambda k: mean_ci95([float(c[k]) for c in cs])  # noqa: E731
            row = dict(scenario=key, label=label, arch=arch, arch_label=arch_label, runs=len(cs),
                       groups="|".join(",".join(sorted(g)) for g in island_groups(arch, key) or ()),
                       two_centre_slots=round(sum(c["two_centre_slots"] for c in cs) / len(cs), 2),
                       two_issuer_applied_slots=round(sum(c["two_issuer_applied_slots"] for c in cs) / len(cs), 2),
                       decisions_in=round(sum(c["decisions_in"] for c in cs) / len(cs), 2),
                       tables_started=round(sum(len(c["tables_started"]) for c in cs) / len(cs), 2),
                       conflicts=sum(c["conflicts"] for c in cs))
            for k in ("fresh_in_window_s12", "fresh_in_window_s34", "command_switches_s34_in", "availability",
                      "fresh_after", "ufls_events_after", "ufls_slots_after", "collapses", "dark_h",
                      "critical_served_share", "served_share", "fairness", "failovers"):
                m, ci = mean(k)
                row[k] = round(m, 4)
                if k in ("fresh_after", "ufls_events_after", "served_share"):
                    row[k + "_ci95"] = round(ci, 4)
            row["passed"] = ok
            rows.append(row)
    return rows


def island_svg(rows, runs):
    labels, values, tips, two, ufls, served, breaks = [], [], [], [], [], [], []
    for r in rows:
        labels.append(f"{r['label']}｜{r['arch_label']}")
        values.append([r["fresh_in_window_s12"], r["fresh_in_window_s34"], r["ufls_slots_after"]])
        span = "全天" if r["scenario"] == "jam" else "分區兩小時內"
        tips.append([f"{r['label']}，{r['arch_label']}：{span} S1、S2 有 {r['fresh_in_window_s12'] * 100:.0f}% 的時槽"
                     f"收到新決策",
                     f"{r['label']}，{r['arch_label']}：{span} S3、S4 有 {r['fresh_in_window_s34'] * 100:.0f}% 的時槽"
                     f"收到新決策；S3、S4 因指令切換饋線 {r['command_switches_s34_in']:.1f} 次",
                     f"{r['label']}，{r['arch_label']}：打擊後 {r['ufls_slots_after'] * 100:.1f}% 的時槽有低頻卸載，"
                     f"每次平均 {r['ufls_events_after']:.1f} 次"])
        two.append(f"{r['two_centre_slots']:.0f}" if r["arch"] in "AB" else "—")
        ufls.append(f"{r['ufls_events_after']:.1f}")
        served.append(f"{r['served_share'] * 100:.1f}%")
        if r["arch"] == "C":
            breaks.append(len(labels) - 1)
    cols = ["S1、S2 收到\n新決策的時槽", "S3、S4 收到\n新決策的時槽", "打擊後有低頻\n卸載的時槽"]
    return matrix(labels, cols, values, tips=tips, breaks=breaks[:-1],
                  text_cols=[("兩個中心同時\n下令的時槽", two), ("打擊後低頻\n卸載次數", ufls), ("用戶用電\n滿足率", served)],
                  title="exp3 孤島上的分區：A、B 會腦裂，C、D 沒有 Q 就維持現狀",
                  subtitle=(f"12 條饋線、4 座變電所，24 小時、凌晨約 3 點打擊後剩 45% 供電；每列 {runs} 次\n"
                            "分區從打擊開始兩小時（前兩欄只看這段）；干擾是全天每條連線 10% 的時間斷線（前兩欄看打擊後到結束）\n"
                            "腦裂：備援收不到中心的心跳就接手，兩個中心各自對連得到的變電所下令"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else ">99%" if 0.995 <= v < 1 else f"{v * 100:.0f}%",
                  heat_header="時槽的比例", legend_label="時槽的比例",
                  desc="各種分區與干擾下，兩組變電所收到新決策的比例、打擊後低頻卸載、兩個中心同時下令的時槽數與用戶用電滿足率")


# ---------------------------------------------------------------- main

def main():
    ap = common.common_args(seeds=5, runs=4)
    ap.add_argument("--part", choices=("consensus", "island", "all"), default="all")
    a = ap.parse_args()
    seeds, runs = (1, 2) if a.quick else (a.seeds, a.runs)
    out = a.out or os.path.join(common.ROOT, "results", "main", "exp3_partition")
    passed = True
    meta_base = {"step": "docs/DESIGN.md section 7, step 4", "seeds": seeds, "runs_per_seed": runs}

    if a.part in ("consensus", "all"):
        jobs = [(key, seed, runs) for key, *_ in CONSENSUS for seed in range(seeds)]
        results = common.run_grid(consensus_job, jobs, a, out)
        if results is not None:
            rows = consensus_rows(results)
            print(f"{'scenario':<8} {'decided':>8} {'theory':>7} {'in win':>6} {'p95 s':>7} {'round':>6} {'resume':>7} "
                  f"{'conflict':>8} {'undec':>5} {'mism':>4}")
            for r in rows:
                print(f"{r['scenario']:<8} {r['decided_share']:>8.2%} {str(r['theory_quorum_up']):>7} "
                      f"{r['commits_in_window']:>6} {str(r['latency_p95_s']):>7} {str(r['mean_round']):>6} "
                      f"{str(r['resume_mean_s']):>7} {r['conflicts']:>8} {r['undecided_heights']:>5} "
                      f"{r['chain_mismatch_runs']:>4}{'' if r['passed'] else '   FAIL'}")
            ok = all(r["passed"] for r in rows)
            passed = passed and ok
            common.write_results(out, rows, {
                "experiment": "exp3_partition", "part": "consensus", **meta_base, "config": GridConfig().to_dict(),
                "heights_per_run": HEIGHTS, "partition_slots": [W0, W1], "flap_s": FLAP_S,
                "pass_rule": "no conflicts anywhere; 5|2 and 6|1: every slot of the partition gets a block and all "
                             "chains match after it; 4|3 and 3|2|2: no commit during the partition, commits resume "
                             "after it; flapping and every delay: all chains complete and equal, every delay: every "
                             "slot gets a block",
                "all_passed": ok,
                "known_issues": [
                    "messages across a partition are dropped, not queued; nothing is re-sent except by the "
                    "protocol's own later rounds and the catch-up of decided heights",
                    "jamming takes a node's link down for both directions; a validator whose link is down at the "
                    "end of the run may still be missing the last heights (not checked)",
                    "the theory column is the share of time at least Q validators are up; a slot can still get a "
                    "block when Q come up only later in the slot, so the measured share can lie above it",
                ]})
            with open(out + ".svg", "w", encoding="utf-8") as fh:
                fh.write(consensus_svg(rows, seeds * runs))
            print("wrote", out + ".svg")
            common.drop_shards(out, a)

    if a.part in ("island", "all"):
        iout = out + "_island"
        jobs = [(seed, run) for seed in range(seeds) for run in range(runs)]
        results = common.run_grid(island_job, jobs, a, iout)
        if results is not None:
            rows = island_rows(results)
            print(f"{'scenario':<9} {'arch':<4} {'S12':>5} {'S34':>5} {'sw34':>5} {'2ctr':>5} {'tables':>6} "
                  f"{'ufls>':>6} {'fresh':>6} {'served':>7} {'coll':>4}")
            for r in rows:
                print(f"{r['scenario']:<9} {r['arch']:<4} {r['fresh_in_window_s12']:>5.2f} "
                      f"{r['fresh_in_window_s34']:>5.2f} {r['command_switches_s34_in']:>5.1f} "
                      f"{r['two_centre_slots']:>5.1f} {r['tables_started']:>6.1f} {r['ufls_events_after']:>6.2f} "
                      f"{r['fresh_after']:>6.3f} {r['served_share']:>7.4f} {r['collapses']:>4.1f}"
                      f"{'' if r['passed'] else '   FAIL'}")
            ok = all(r["passed"] for r in rows)
            passed = passed and ok
            common.write_results(iout, rows, {
                "experiment": "exp3_partition", "part": "island", **meta_base, "config": GridConfig().to_dict(),
                "window_s": WINDOW_S, "jam": {"link_loss_model": "outage", "link_loss_p": 0.10},
                "pass_rule": "C: no conflicts, no collapses; split: C decides nothing during the partition, A and B "
                             "run two active centres at once; majority and subs_cut: C's S1 and S2 get a fresh "
                             "decision every slot while S3 and S4 switch nothing by command; subs_cut: B's S3 and S4 "
                             "start their tables while S1 and S2 follow the centre",
                "all_passed": ok,
                "known_issues": [
                    "C stands for D: the same code; they differ in who runs the validators (experiment 6)",
                    "in the split, each of A's and B's centres plans with its newest reports, which for the other "
                    "half of the island stop at the partition; substations execute whichever signed command fits "
                    "their state",
                    "the jamming column is over the whole day after the strike, not a two-hour window",
                ]})
            with open(iout + ".svg", "w", encoding="utf-8") as fh:
                fh.write(island_svg(rows, seeds * runs))
            print("wrote", iout + ".svg")
            common.drop_shards(iout, a)

    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
