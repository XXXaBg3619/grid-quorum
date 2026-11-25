"""Experiment 2 (docs/DESIGN.md section 5): decision nodes that stop answering.

Part consensus (run_committee, no grid): committees of n = 4 (f = 1) and n = 7
(f = 2) with k = 0 .. f + 2 validators offline (the last k, so their turns as
proposer come in a row: the worst case for latency) for one hour, from slot 6
to slot 18 of 36 five-minute slots; every message lost with probability p = 0
or 0.1. Checks pass when:

  * k <= f: every slot in the outage gets a committed block, and at p = 0 each
    first commit comes within theory.commit_latency_bound for the number of
    offline proposers in a row at its height;
  * k > f: nothing is committed during the outage (the committee stops instead
    of deciding without a quorum), and commits resume after it;
  * always: no two different blocks at one height, and after the run every
    validator, the returning ones too, holds the same complete chain.

Part island (the default island event: 24 hours, a strike near hour 3, 45 % of
the peak after it): at the strike, A and B lose their dispatch centre (within
what they tolerate: the backup takes over) or both centres (beyond), C and D
lose f = 2 validators (within) or f + 1 = 3 (beyond), for 30 minutes, 2 hours
or the rest of the day. C stands for D: the same code, a different owner per
node. Checks pass when:

  * within: A and B fail over and keep deciding (at most one slot missed); C
    keeps deciding in every slot, exactly as without the loss;
  * beyond: C commits nothing during the outage, its substations hold (no
    command switches), and decisions resume within two dispatch periods after
    the validators return (the returning validators' own proposals are refused
    by the report-inclusion rule until each substation's next report reaches
    them, at most one period; a validator that stayed up gets its turn as
    proposer within a round or two); A decides nothing either and resumes
    within the failover time plus one period; B's substations all run their
    pre-signed tables (the outage outlasts cfg.fallback_after_s);
  * no island collapses anywhere.

Writes <out>.csv, .meta.json, .svg (consensus) and <out>_island.* and exits
with status 1 if a check fails.

  python3 experiments/exp2_offline.py [--part consensus|island|all] [--quick] [--seeds 5] [--runs 4]
      [--workers 4] [--out results/main/exp2_offline] [--shard I/N | --merge N]
"""
from __future__ import annotations

import math
import os
import sys
from collections import defaultdict

import common
from gridq.config import GridConfig
from gridq.consensus import run_committee
from gridq.events import EventKind
from gridq.faults import Offline
from gridq.hashing import canonical_hash
from gridq.loads import Loads, default_island
from gridq.metrics import after_event, island_metrics, mean_ci95, window_metrics
from gridq.power import supply_scenario
from gridq.sim import Simulator
from gridq.svgchart import matrix
from gridq.theory import commit_latency_bound

NS = (4, 7)
NETS = ((0.0, 0), (0.1, 0), (0.1, 2))            # (message loss p, resends): no loss; 10 %; 10 % with 2 resends
HEIGHTS, OUT_FROM, OUT_TO = 36, 6, 18            # consensus part: slots, outage [6, 18) = one hour
ARCHES = (("A", "A 中央＋備援"), ("B", "B 中央＋在地檢查"), ("C", "C、D 委員會"))
LOST = {"A": (("centre",), ("centre", "backup")), "B": (("centre",), ("centre", "backup")),
        "C": (("v5", "v6"), ("v4", "v5", "v6"))}
LEVELS = (("within", "容錯內"), ("beyond", "超過容錯"))
DURATIONS = (("30m", "30 分鐘", 1800.0), ("2h", "2 小時", 7200.0), ("day", "到當天結束", math.inf))
LOST_LABEL = {("A", "within"): "失去調度中心", ("A", "beyond"): "失去兩個中心",
              ("B", "within"): "失去調度中心", ("B", "beyond"): "失去兩個中心",
              ("C", "within"): "失去 2 個驗證者", ("C", "beyond"): "失去 3 個驗證者"}


def offline_set(n, k):
    return [f"v{i}" for i in range(n - k, n)]


def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


# ---------------------------------------------------------------- consensus part

def consensus_job(args):
    n, k, (p, retries), seed, runs = args
    cfg = GridConfig(n_validators=n, link_loss_p=p, link_retries=retries, seed=seed)
    period = cfg.dispatch_period_s
    t0, t1 = OUT_FROM * period, OUT_TO * period
    down = set(offline_set(n, k))
    out = defaultdict(int, n=n, k=k, p=p, retries=retries)
    out["latency"], out["resume"] = [], []
    for run in range(runs):
        com = run_committee(cfg, fault=Offline(down, t0, t1) if k else None, run=run, heights=HEIGHTS, drain_s=1800.0)
        commits = [c for d in com.decisions.values() for c in d.values()]
        chains = {tuple(e["hash"] for e in com.nodes[v].chain) for v in com.names}
        slots = {e["slot"] for e in com.nodes[com.names[0]].chain}
        out["runs"] += 1
        out["slots_in_window"] += OUT_TO - OUT_FROM
        out["decided_in_window"] += len(slots & set(range(OUT_FROM, OUT_TO)))
        out["decided"] += len(slots)
        out["commits_in_window"] += sum(1 for c in commits if t0 <= c["t"] < t1)
        out["conflicts"] += len(com.conflicting_heights())
        out["undecided"] += len(com.undecided_heights())
        out["chain_mismatch"] += len(chains) > 1
        after = [c["t"] for c in commits if c["t"] >= t1]
        if k > (n - 1) // 3:
            out["resume"].append(round(min(after) - t1, 3) if after else None)
        for h, c in com.first_commits().items():
            start = min(com.starts[h].values())
            if not (t0 <= start and c["t"] < t1) or c["latency"] is None:
                continue
            out["latency"].append(round(c["latency"], 4))
            silent = 0
            while com.proposer(h, silent) in down:
                silent += 1
            st = com.starts[h]
            clean = len(st) == n - k and max(st.values()) - min(st.values()) < 1e-9
            if p == 0.0 and clean and c["latency"] > commit_latency_bound(cfg, silent) + 1e-9:
                out["over_bound"] += 1
    return dict(out)


def consensus_rows(results):
    cells = defaultdict(list)
    for r in results:
        cells[(r["n"], r["k"], r["p"], r["retries"])].append(r)
    rows = []
    for n in NS:
        f = (n - 1) // 3
        for k in range(f + 3):
            for p, retries in NETS:
                rs = cells[(n, k, p, retries)]
                tot = lambda key: sum(r.get(key, 0) for r in rs)  # noqa: E731
                lat = [x for r in rs for x in r["latency"]]
                resume = [x for r in rs for x in r["resume"]]
                ok = tot("conflicts") == 0 and tot("undecided") == 0 and tot("chain_mismatch") == 0
                if k <= f and p == 0.0:
                    ok = ok and tot("decided_in_window") == tot("slots_in_window") and tot("over_bound") == 0
                elif k > f:
                    ok = ok and tot("commits_in_window") == 0 and None not in resume
                rows.append(dict(
                    n=n, f=f, quorum=2 * n // 3 + 1, k=k, beyond_f=k > f, p=p, resends=retries, runs=tot("runs"),
                    slots_in_window=tot("slots_in_window"), decided_in_window=tot("decided_in_window"),
                    decided_share_in_window=round(tot("decided_in_window") / tot("slots_in_window"), 4),
                    commits_in_window=tot("commits_in_window"), decided_slots=tot("decided"),
                    latency_p50_s=round(quantile(lat, 0.5), 3) if lat else "",
                    latency_p95_s=round(quantile(lat, 0.95), 3) if lat else "",
                    latency_max_s=round(max(lat), 3) if lat else "", over_bound=tot("over_bound"),
                    resume_mean_s=round(sum(x for x in resume if x is not None) / len(resume), 2) if resume else "",
                    resume_max_s=round(max((x for x in resume if x is not None), default=float("nan")), 2)
                    if resume else "",
                    conflicts=tot("conflicts"), undecided_heights=tot("undecided"),
                    chain_mismatch_runs=tot("chain_mismatch"), passed=ok))
    return rows


def net_label(p, retries):
    return "無遺失" if p == 0 else f"遺失 {p:.0%}" + (f"，重送 {retries} 次" if retries else "，不重送")


def consensus_svg(rows, runs):
    by = {(r["n"], r["k"], r["p"], r["resends"]): r for r in rows}
    labels, values, tips, lat, resume, breaks = [], [], [], [], [], []
    secs = lambda v: "—" if v == "" else (f"{v / 60:.0f} 分鐘" if v >= 600 else f"{v:.1f} 秒")  # noqa: E731
    for n in (7, 4):
        f = (n - 1) // 3
        for k in range(f + 3):
            rs = [by[(n, k, p, rt)] for p, rt in NETS]
            where = "超過 f" if k > f else ("無" if k == 0 else "f 以內")
            labels.append(f"n = {n}（f = {f}）｜{k} 個離線（{where}）")
            values.append([r["decided_share_in_window"] for r in rs])
            tips.append([f"n = {n}，{k} 個離線，{net_label(p, rt)}：離線那一小時的 {r['slots_in_window']} 個時槽中"
                         f"有 {r['decided_in_window']} 個提交了區塊；期間提交 {r['commits_in_window']} 次"
                         for (p, rt), r in zip(NETS, rs)])
            lat.append(" ／ ".join(secs(r["latency_p95_s"]) for r in rs))
            resume.append("—" if k <= f else " ／ ".join(secs(r["resume_mean_s"]) for r in rs))
        breaks.append(len(labels) - 1)
    cols = [f"離線期間有區塊的時槽\n{net_label(p, rt)}" for p, rt in NETS]
    return matrix(labels, cols, values, tips=tips, breaks=breaks[:-1],
                  text_cols=[("離線期間提交延遲 p95\n三種網路依序", lat), ("恢復後到第一次提交\n三種網路依序", resume)],
                  title="exp2 驗證者離線：f 個以內照常提交，超過就停下來，回來後接上",
                  subtitle=(f"36 個 5 分鐘時槽，第 6 到 18 個時槽（一小時）有 k 個驗證者完全不收不送；每格 {runs} 次\n"
                            "離線的是最後 k 個，輪到它們當提案者時連續逾時（延遲最差的情況）；回來的驗證者從其他人補齊區塊\n"
                            "f 個離線時剩下的正好是 Q 個，任何一則訊息遺失都湊不滿：不重送時大多數時槽做不出決策，重送就恢復"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else ">99%" if 0.995 <= v < 1 else f"{v * 100:.0f}%",
                  heat_header="時槽的比例", legend_label="時槽的比例",
                  desc="不同委員會大小與離線節點數下，離線期間有區塊提交的時槽比例、提交延遲，以及恢復後到第一次提交的時間")


# ---------------------------------------------------------------- island part

def scenarios():
    yield ("honest", "", "", None)
    for level, _ in LEVELS:
        for dur, _, secs in DURATIONS:
            yield (f"{level}_{dur}", level, dur, secs)


def island_job(args):
    seed, run = args
    base = GridConfig(seed=seed)
    specs = default_island()
    supply = supply_scenario(base, Loads(base, specs, run).peak_kw(), run)
    strike = supply.strike()
    out = []
    for arch, _ in ARCHES:
        cfg = base.with_(arch=arch)
        for key, level, dur, secs in scenarios():
            fault = None
            if level:
                lost = LOST[arch][0 if level == "within" else 1]
                fault = Offline(lost, start=strike, end=strike + secs)
            sim = Simulator(cfg, fault, run, specs=specs, supply=supply).run()
            m = island_metrics(sim.log)
            m.update(after_event(sim.log, cfg, strike, len(sim.link.subs)))
            m.update(window_metrics(sim.log, cfg, strike, strike + (secs if level else 0.0)))
            m["decisions"] = canonical_hash([[e.data["expires"], e.data["substation"], e.data["feeder"],
                                              e.data["target"]] for e in sim.log.of_kind(EventKind.COMMAND_ISSUED)])
            m["conflicts"] = len(sim.link.com.conflicting_heights()) if arch == "C" else 0
            out.append(dict(arch=arch, scenario=key, level=level, duration=dur, seed=seed, run=run, **m))
    return out


def island_rows(results):
    cells = defaultdict(list)
    for res in results:
        for c in res:
            cells[(c["arch"], c["scenario"])].append(c)
    honest = {(c["seed"], c["run"]): c["decisions"] for c in cells[("C", "honest")]}
    n_slots, period = GridConfig().n_dispatches, GridConfig().dispatch_period_s
    rows = []
    for key, level, dur, secs in scenarios():
        for arch, arch_label in ARCHES:
            cs = cells[(arch, key)]
            every = lambda pred: all(pred(c) for c in cs)  # noqa: E731
            ok = every(lambda c: c["collapses"] == 0 and c["conflicts"] == 0)
            if level == "within":
                if arch in "AB":
                    ok = ok and every(lambda c: c["availability"] >= 1.0 - 1.0 / n_slots and c["failovers"] >= 1)
                else:
                    ok = ok and every(lambda c: c["availability"] == 1.0
                                      and c["decisions"] == honest[(c["seed"], c["run"])])
            elif level == "beyond":
                fin = math.isfinite(secs)
                if arch == "C":
                    ok = ok and every(lambda c: c["decisions_in"] == 0 and c["switched_in:command"] == 0)
                    ok = ok and (not fin or every(lambda c: c["resume_s"] != "" and c["resume_s"] <= 2 * period))
                elif arch == "A":
                    ok = ok and every(lambda c: c["decisions_in"] == 0)
                    ok = ok and (not fin or every(lambda c: c["resume_s"] != ""
                                                  and c["resume_s"] <= GridConfig().failover_s + period))
                else:
                    ok = ok and every(lambda c: c["fallback_starts"] == 4)
            mean = lambda k: mean_ci95([float(c[k]) for c in cs])  # noqa: E731
            resumes = [c["resume_s"] for c in cs if c["resume_s"] != ""]
            row = dict(scenario=key, level=level, duration=dur, arch=arch, arch_label=arch_label,
                       lost="+".join(LOST[arch][0 if level == "within" else 1]) if level else "", runs=len(cs),
                       decisions_in=round(sum(c["decisions_in"] for c in cs) / len(cs), 2),
                       command_switches_in=round(sum(c["switched_in:command"] for c in cs) / len(cs), 2),
                       table_switches_in=round(sum(c["switched_in:table"] for c in cs) / len(cs), 2),
                       resume_mean_s=round(sum(resumes) / len(resumes), 2) if resumes else "",
                       resume_max_s=round(max(resumes), 2) if resumes else "",
                       decision_p95_s=round(max(c["decision_p95_s"] for c in cs if c["decision_p95_s"] != ""), 3),
                       same_decisions_as_honest=round(sum(c["decisions"] == honest[(c["seed"], c["run"])]
                                                          for c in cs) / len(cs), 4) if arch == "C" else "")
            for k in ("availability", "fresh_after", "ufls_events_after", "ufls_slots_after", "collapses", "dark_h",
                      "critical_served_share", "served_share", "fairness", "failovers", "fallback_starts"):
                m, ci = mean(k)
                row[k] = round(m, 4)
                if k in ("availability", "fresh_after", "ufls_events_after", "served_share"):
                    row[k + "_ci95"] = round(ci, 4)
            row["passed"] = ok
            rows.append(row)
    return rows


def island_svg(rows, runs):
    labels, values, tips, resume, served, crit, breaks = [], [], [], [], [], [], []
    dur_label = {d: lab for d, lab, _ in DURATIONS}
    secs = lambda v: "—" if v == "" else (f"{v / 60:.1f} 分鐘" if v >= 120 else f"{v:.0f} 秒")  # noqa: E731
    for r in rows:
        if r["level"]:
            what = f"{LOST_LABEL[(r['arch'], r['level'])]}・{dur_label[r['duration']]}"
        else:
            what = "沒有離線"
        labels.append(f"{what}｜{r['arch_label']}")
        values.append([r["fresh_after"], r["ufls_slots_after"]])
        tips.append([f"{what}，{r['arch_label']}：打擊後 {r['fresh_after'] * 100:.1f}% 的（變電所, 時槽）收到新決策；"
                     f"離線期間平均做出 {r['decisions_in']:.1f} 個決策",
                     f"{what}，{r['arch_label']}：打擊後 {r['ufls_slots_after'] * 100:.1f}% 的時槽有低頻卸載，"
                     f"每次平均 {r['ufls_events_after']:.1f} 次"])
        resume.append(secs(r["resume_mean_s"]) if r["level"] == "beyond" and r["duration"] != "day" else "—")
        served.append(f"{r['served_share'] * 100:.1f}%")
        crit.append(f"{r['critical_served_share'] * 100:.1f}%")
        if r["arch"] == "C":
            breaks.append(len(labels) - 1)
    return matrix(labels, ["打擊後收到\n新決策的時槽", "打擊後有低頻卸載\n的調度時槽"], values, tips=tips,
                  breaks=breaks[:-1],
                  text_cols=[("節點回來後\n第一個決策", resume), ("用戶用電\n滿足率", served), ("關鍵饋線\n供電時間", crit)],
                  title="exp2 打擊時失去決策節點：超過容錯時 C、D 停下來維持現狀，B 跑預簽輪停表",
                  subtitle=(f"12 條饋線、4 座變電所，24 小時、凌晨約 3 點打擊後剩 45% 供電；每列 {runs} 次\n"
                            "容錯內：A、B 失去調度中心（備援接手），C、D 失去 2 個驗證者；超過容錯：A、B 失去兩個中心，"
                            "C、D 失去 3 個驗證者\n離線從打擊開始，持續 30 分鐘、2 小時或到當天結束"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else ">99%" if 0.995 <= v < 1 else f"{v * 100:.0f}%",
                  heat_header="時槽的比例", legend_label="時槽的比例",
                  desc="打擊時失去不同數量的決策節點、離線不同時間時，各架構打擊後收到新決策的比例、低頻卸載的時槽比例、"
                       "恢復時間、用戶用電滿足率與關鍵饋線供電時間")


# ---------------------------------------------------------------- main

def main():
    ap = common.common_args(seeds=5, runs=4)
    ap.add_argument("--part", choices=("consensus", "island", "all"), default="all")
    a = ap.parse_args()
    seeds, runs = (1, 2) if a.quick else (a.seeds, a.runs)
    out = a.out or os.path.join(common.ROOT, "results", "main", "exp2_offline")
    passed = True
    meta_base = {"step": "docs/DESIGN.md section 7, step 4", "seeds": seeds, "runs_per_seed": runs}

    if a.part in ("consensus", "all"):
        jobs = [(n, k, net, seed, runs) for n in NS for k in range((n - 1) // 3 + 3) for net in NETS
                for seed in range(seeds)]
        results = common.run_grid(consensus_job, jobs, a, out)
        if results is not None:
            rows = consensus_rows(results)
            print(f"{'n':>2} {'k':>2} {'p':>4} {'rs':>2} {'in window':>9} {'commits':>7} {'p95 s':>6} {'max s':>6} "
                  f"{'over':>4} {'resume s':>8} {'conflict':>8} {'undec':>5}")
            for r in rows:
                print(f"{r['n']:>2} {r['k']:>2} {r['p']:>4} {r['resends']:>2} {r['decided_share_in_window']:>9.2%} "
                      f"{r['commits_in_window']:>7} {str(r['latency_p95_s']):>6} {str(r['latency_max_s']):>6} "
                      f"{r['over_bound']:>4} {str(r['resume_mean_s']):>8} {r['conflicts']:>8} "
                      f"{r['undecided_heights']:>5}{'' if r['passed'] else '   FAIL'}")
            ok = all(r["passed"] for r in rows)
            passed = passed and ok
            common.write_results(out, rows, {
                "experiment": "exp2_offline", "part": "consensus", **meta_base, "config": GridConfig().to_dict(),
                "heights_per_run": HEIGHTS, "outage_slots": [OUT_FROM, OUT_TO], "committee_sizes": list(NS),
                "networks": [{"link_loss_p": p, "link_retries": rt} for p, rt in NETS],
                "offline": "the last k validators (v{n-k} .. v{n-1})",
                "pass_rule": "k <= f at p = 0: every slot of the outage gets a block, within commit_latency_bound; "
                             "k > f: no commit during the outage, commits resume after it; always no conflicts and "
                             "every validator ends with the same complete chain",
                "all_passed": ok,
                "known_issues": [
                    "an offline validator's timers keep running, so it comes back in about the round the others "
                    "reached; after a long stop round timeouts have grown (timeout_delta_s per round), which sets "
                    "the time to the first commit after recovery (column resume_mean_s)",
                    "the latency bound is checked only at p = 0 for heights that every online validator started "
                    "at the same moment; with loss it is reported, not checked",
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
            print(f"{'scenario':<12} {'arch':<4} {'fresh':>6} {'ufls>':>6} {'dec in':>6} {'cmd in':>6} {'table':>6} "
                  f"{'resume':>7} {'served':>7} {'crit':>6} {'coll':>4}")
            for r in rows:
                print(f"{r['scenario']:<12} {r['arch']:<4} {r['fresh_after']:>6.3f} {r['ufls_events_after']:>6.2f} "
                      f"{r['decisions_in']:>6.1f} {r['command_switches_in']:>6.1f} {r['table_switches_in']:>6.1f} "
                      f"{str(r['resume_mean_s']):>7} {r['served_share']:>7.4f} {r['critical_served_share']:>6.3f} "
                      f"{r['collapses']:>4.1f}{'' if r['passed'] else '   FAIL'}")
            ok = all(r["passed"] for r in rows)
            passed = passed and ok
            common.write_results(iout, rows, {
                "experiment": "exp2_offline", "part": "island", **meta_base, "config": GridConfig().to_dict(),
                "lost": {a: {"within": list(w), "beyond": list(b)} for a, (w, b) in LOST.items()},
                "durations_s": {d: (None if math.isinf(s) else s) for d, _, s in DURATIONS},
                "pass_rule": "within: A, B fail over (at most one slot missed), C decides every slot as without the "
                             "loss; beyond: C commits nothing and switches no feeder by command during the outage and "
                             "resumes within two periods, A decides nothing and resumes within failover_s plus one "
                             "period, B's four substations run their tables; no collapses",
                "all_passed": ok,
                "known_issues": [
                    "C stands for D: the same code; they differ in who runs the validators (experiment 6)",
                    "a substation that holds keeps its feeders as they are and sheds only by UFLS; the island's "
                    "load and supply keep changing, so a long hold can trip UFLS that a live decision would avoid",
                    "both centres of A come back as standby; the one that hears no heartbeat for failover_s takes "
                    "over, so A's resumption includes the failover time",
                    "a validator back from an outage missed the substations' reports while it was away; until each "
                    "substation's next report (at most one dispatch period) its own proposals leave reports out "
                    "and the others refuse them (report-inclusion rule), so C resumes when a validator that stayed "
                    "up proposes (up to about 400 s after a two-hour stop, column resume_max_s). Validators do not "
                    "pass reports to each other, as a CometBFT mempool would on reconnect",
                ]})
            with open(iout + ".svg", "w", encoding="utf-8") as fh:
                fh.write(island_svg(rows, seeds * runs))
            print("wrote", iout + ".svg")
            common.drop_shards(iout, a)

    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
