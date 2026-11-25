"""Experiment 1 (docs/DESIGN.md section 5): validators that double-sign.

b of the n validators are compromised (faults.Equivocate): as the proposer of a
round, a compromised validator sends one block to half of the honest
validators and a second block to the other half, and in such a round every
compromised validator prevotes and precommits the first block to the first
half and the second block to the second. That is the attacker's best split:
within f, the protocol must still commit at most one block per height, and it
must keep committing; with f + 1 the two halves can each reach Q.

Part consensus (run_committee, no grid): n = 4 (f = 1) and n = 7 (f = 2),
b = 0 .. f + 1 (the last b validators), every message lost with probability
p = 0 or 0.1, heights one hour apart. Checks pass when:

  * b <= f: no two honest validators commit different blocks at any height,
    and every honest validator commits every height;
  * every pair of conflicting commit proofs shares at least f + 1 signers
    (quorum intersection), and when both are from the same round, all the
    shared signers are compromised: accountable safety, two proofs name the
    double signers;
  * no honest validator logs evidence against an honest one;
  * b = f + 1 at p = 0: conflicting commits do happen (otherwise the zeros
    above would prove nothing).

Part island (architecture C; D runs the same code, only who runs the
validators differs): the default island event of check_island (24 hours, a
strike near hour 3, 45 % of the peak after it) with b = 0 .. 3 of 7 validators
compromised, with the report-inclusion rule (cfg.report_inclusion_s = 60 s: an
honest validator prevotes nil on a new proposal that leaves out a substation
report it has held that long before the slot) and without it. Here the second
block is the proposer's block without the fresh state reports: equally valid,
its commands recomputed for the older state. Checks pass when:

  * b <= 2, with or without the rule: no conflicting commits, and every honest
    validator commits every height;
  * b <= 2 with the rule: no attacker block committed, and the island does as
    well as without the attack in the same run (UFLS after the strike,
    collapses, critical feeders);
  * b = 2 without the rule: attacker blocks are committed (the attack within f
    works there, which is why the rule exists).

b = 3 (beyond f) is shown, not checked: the protocol promises nothing there.

Writes <out>.csv, <out>.meta.json, <out>.svg (consensus) and <out>_island.csv,
.meta.json, .svg (island), and exits with status 1 if a check fails.

  python3 experiments/exp1_equivocation.py [--part consensus|island|all] [--quick] [--seeds 5] [--runs 4]
      [--heights 50] [--workers 4] [--out results/main/exp1_equivocation] [--shard I/N | --merge N]
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
from gridq.faults import Equivocate
from gridq.loads import Loads, default_island
from gridq.metrics import after_event, island_metrics, mean_ci95
from gridq.power import supply_scenario
from gridq.sim import Simulator
from gridq.svgchart import matrix

NS = (4, 7)
PS = (0.0, 0.1)
SLOT_S = 3600.0                  # consensus part: one height an hour, so heights never overlap
ISLAND_B = (0, 1, 2, 3)
RULES = (("rule", "有「報告必收」規則", 60.0), ("no_rule", "沒有這條規則", math.inf))


def bad_set(n, b):
    """The last b of n validators."""
    return [f"v{i}" for i in range(n - b, n)]


def name(actor):
    return actor.split(":")[1]


def double_signers(log):
    """Validators that sent two different proposals or votes in one round (ground truth)."""
    return {name(e.actor) for e in log.of_kind(EventKind.FAULT_ACTION)
            if e.data.get("action") in ("proposal", "prevote", "precommit") and len(e.data.get("sent", {})) > 1}


def accused(log, honest):
    """Validators an honest validator logged evidence against (two signed votes for one round)."""
    return {e.data["offender"] for e in log.of_kind(EventKind.EVIDENCE) if name(e.actor) in honest}


def honest_firsts(com, honest):
    """Per height: (seconds from the height's first round-0 start to the first honest commit, its round)."""
    out = {}
    for h, d in com.decisions.items():
        cs = [c for n, c in d.items() if n in honest]
        if cs and com.starts.get(h):
            first = min(cs, key=lambda c: c["t"])
            out[h] = (first["t"] - min(com.starts[h].values()), min(c["round"] for c in cs))
    return out


def conflicts(com, honest, bad):
    """Per conflicting height: (same round?, shared signers, all of them compromised?)."""
    out = []
    for h in com.conflicting_heights(honest):
        proofs = {}
        for n in honest:
            if n in com.decisions[h]:
                p = com.nodes[n].proofs[h - 1]
                proofs.setdefault(p["block"], p)
        a, b = list(proofs.values())[:2]
        shared = set(a["sigs"]) & set(b["sigs"])
        out.append((a["round"] == b["round"], len(shared), shared <= set(bad)))
    return out


def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


# ---------------------------------------------------------------- consensus part

def consensus_job(args):
    """All runs of one (n, b, p, seed): counts and latencies."""
    n, b, p, seed, runs, heights = args
    cfg = GridConfig(n_validators=n, link_loss_p=p, seed=seed, dispatch_period_s=SLOT_S)
    bad = bad_set(n, b)
    out = defaultdict(int, n=n, b=b, p=p)
    out["latency"], out["rounds"], out["conflicts"] = [], [], []
    for run in range(runs):
        com = run_committee(cfg, fault=Equivocate(bad) if b else None, run=run, heights=heights, drain_s=SLOT_S)
        honest = com.honest()
        firsts = honest_firsts(com, honest)
        conf = conflicts(com, honest, bad)
        doubled, caught = double_signers(com.log), accused(com.log, honest)
        out["runs"] += 1
        out["heights"] += len(com.starts)
        out["fork_runs"] += bool(conf)
        out["undecided"] += len(com.undecided_heights(honest))
        out["split_rounds"] += len(com.fault.pairs) if b else 0
        out["double_signers"] += len(doubled)
        out["caught"] += len(caught & doubled)
        out["false_accusations"] += len(caught - set(bad))
        out["conflicts"] += conf
        out["latency"] += [round(lat, 4) for lat, _ in firsts.values()]
        out["rounds"] += [r for _, r in firsts.values()]
    return dict(out)


def consensus_rows(results):
    cells = defaultdict(list)
    for r in results:
        cells[(r["n"], r["b"], r["p"])].append(r)
    rows = []
    for n in NS:
        f, q = (n - 1) // 3, 2 * n // 3 + 1
        for b in range(f + 2):
            for p in PS:
                rs = cells[(n, b, p)]
                tot = lambda k: sum(r[k] for r in rs)  # noqa: E731
                conf = [c for r in rs for c in r["conflicts"]]
                lat = [x for r in rs for x in r["latency"]]
                rounds = [x for r in rs for x in r["rounds"]]
                same = [c for c in conf if c[0]]
                ok = (tot("false_accusations") == 0 and all(c[1] >= f + 1 for c in conf)
                      and all(c[2] for c in same))
                if b <= f:
                    ok = ok and not conf and tot("undecided") == 0
                if b == f + 1 and p == 0.0:
                    ok = ok and len(conf) > 0
                rows.append(dict(
                    n=n, f=f, quorum=q, b=b, beyond_f=b > f, p=p, runs=tot("runs"), heights=tot("heights"),
                    conflict_heights=len(conf), conflict_share=round(len(conf) / tot("heights"), 4),
                    fork_runs=tot("fork_runs"),
                    undecided_honest_heights=tot("undecided"),
                    undecided_share=round(tot("undecided") / tot("heights"), 4),
                    split_rounds=tot("split_rounds"), double_signers=tot("double_signers"), caught=tot("caught"),
                    caught_share=round(tot("caught") / tot("double_signers"), 4) if tot("double_signers") else "",
                    false_accusations=tot("false_accusations"),
                    conflicts_same_round=len(same), conflicts_accountable=sum(1 for c in same if c[2]),
                    min_shared_signers=min((c[1] for c in conf), default=""),
                    latency_p50_s=round(quantile(lat, 0.5), 3), latency_p95_s=round(quantile(lat, 0.95), 3),
                    latency_max_s=round(max(lat), 3) if lat else "",
                    mean_round=round(sum(rounds) / len(rounds), 4) if rounds else "", passed=ok))
    return rows


def consensus_svg(rows, runs, heights):
    by = {(r["n"], r["b"], r["p"]): r for r in rows}
    labels, values, tips, lat, caught, blame, breaks = [], [], [], [], [], [], []
    secs = lambda v: f"{v / 60:.0f} 分鐘" if v >= 600 else f"{v:.1f} 秒"  # noqa: E731
    for n in (7, 4):
        f = (n - 1) // 3
        for b in range(f + 2):
            rs = [by[(n, b, p)] for p in PS]
            where = "超過 f" if b > f else ("無" if b == 0 else "f 以內")
            labels.append(f"n = {n}（f = {f}）｜{b} 個作惡（{where}）")
            values.append([r["conflict_share"] for r in rs] + [r["undecided_share"] for r in rs])
            tips.append([f"n = {n}，{b} 個作惡，訊息遺失 {p:.0%}：{r['heights']} 個高度中有 {r['conflict_heights']} 個"
                         f"被誠實節點提交了兩個不同的區塊（{r['fork_runs']} 次分叉）" for p, r in zip(PS, rs)] +
                        [f"n = {n}，{b} 個作惡，訊息遺失 {p:.0%}：{r['undecided_honest_heights']} 個高度有誠實節點"
                         f"始終沒有提交" for p, r in zip(PS, rs)])
            lat.append(" ／ ".join(secs(r["latency_p95_s"]) for r in rs))
            caught.append("—" if b == 0 else " ／ ".join(f"{r['caught']}/{r['double_signers']}" for r in rs))
            blame.append("—" if b <= f else " ／ ".join(f"{r['conflicts_accountable']}/{r['conflict_heights']}"
                                                       for r in rs))
        breaks.append(len(labels) - 1)
    cols = ["兩個區塊都被提交\n遺失 0%", "兩個區塊都被提交\n遺失 10%", "誠實節點沒提交\n遺失 0%", "誠實節點沒提交\n遺失 10%"]
    return matrix(labels, cols, values, tips=tips, breaks=breaks[:-1],
                  text_cols=[("提交延遲 p95\n遺失 0% ／ 10%", lat), ("被誠實節點抓到的雙簽者\n遺失 0% ／ 10%", caught),
                             ("兩份證明的共同簽名者\n都是作惡者的分叉", blame)],
                  title="exp1 作惡節點雙簽：f 個以內不會分叉，f + 1 個就會",
                  subtitle=(f"作惡的提案者把兩個區塊各送給一半的誠實節點，作惡者在兩邊各投一票；"
                            f"每格 {runs} 次、每次 {heights} 個高度\n"
                            "格子是高度的比例；雙簽者以「一人次」計，被抓到 = 有誠實節點記下他同一輪的兩張簽名票\n"
                            "分叉時沒有誠實節點同時看到兩邊的票，但兩份提交證明至少共用 f + 1 個簽名，正好點名作惡者"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else f"{v * 100:.0f}%",
                  heat_header="高度的比例", legend_label="高度的比例",
                  desc="不同委員會大小與作惡節點數下，誠實節點提交兩個不同區塊的高度比例、沒有提交的高度比例、提交延遲和抓到的雙簽者")


# ---------------------------------------------------------------- island part

def island_job(args):
    """One island event with b compromised validators, with or without the inclusion rule."""
    b, rule, seed, run = args
    rule_s = next(s for k, _, s in RULES if k == rule)
    cfg = GridConfig(seed=seed, arch="C", report_inclusion_s=rule_s)
    specs = default_island()
    supply = supply_scenario(cfg, Loads(cfg, specs, run).peak_kw(), run)
    strike = supply.strike()
    sim = Simulator(cfg, Equivocate(bad_set(7, b)) if b else None, run, specs=specs, supply=supply).run()
    com, bad = sim.link.com, bad_set(7, b)
    honest = com.honest()
    second = {pair[1] for pair in sim.fault.pairs.values()} if b else set()
    committed = {c["block"] for d in com.decisions.values() for n, c in d.items() if n in honest}
    firsts = honest_firsts(com, honest)
    doubled, caught = double_signers(sim.log), accused(sim.log, honest)
    m = island_metrics(sim.log)
    m.update(after_event(sim.log, cfg, strike, len(sim.link.subs)))
    return dict(b=b, rule=rule, seed=seed, run=run, heights=len(com.starts),
                conflicts=len(com.conflicting_heights(honest)), undecided=len(com.undecided_heights(honest)),
                split_rounds=len(sim.fault.pairs) if b else 0, attacker_commits=len(second & committed),
                double_signers=len(doubled), caught=len(caught & doubled), false_accusations=len(caught - set(bad)),
                latency_p95_s=round(quantile([lat for lat, _ in firsts.values()], 0.95), 4),
                extra_rounds=sum(r for _, r in firsts.values()),
                **{k: m[k] for k in ("availability", "fresh_after", "ufls_events_after", "ufls_slots_after",
                                     "collapses", "critical_served_share", "served_share", "issued", "executed")})


def island_rows(results):
    cells = defaultdict(list)
    for c in results:
        cells[(c["rule"], c["b"])].append(c)
    rows = []
    for rule, rule_label, rule_s in RULES:
        ref = {(c["seed"], c["run"]): c for c in cells[(rule, 0)]}
        for b in ISLAND_B:
            cs = cells[(rule, b)]
            tot = lambda k: sum(c[k] for c in cs)  # noqa: E731
            worse = sum(1 for c in cs if c["ufls_events_after"] > ref[(c["seed"], c["run"])]["ufls_events_after"]
                        or c["collapses"] > ref[(c["seed"], c["run"])]["collapses"]
                        or c["critical_served_share"] < ref[(c["seed"], c["run"])]["critical_served_share"])
            ok = True
            if b <= 2:
                ok = tot("conflicts") == 0 and tot("undecided") == 0
                if rule == "rule":
                    ok = ok and tot("attacker_commits") == 0 and worse == 0
                elif b == 2:
                    ok = ok and tot("attacker_commits") > 0
            mean = lambda k: mean_ci95([float(c[k]) for c in cs])  # noqa: E731
            row = dict(rule=rule, rule_label=rule_label, report_inclusion_s="inf" if math.isinf(rule_s) else rule_s,
                       b=b, beyond_f=b > 2, runs=len(cs), heights=tot("heights"),
                       conflict_heights=tot("conflicts"), fork_runs=sum(1 for c in cs if c["conflicts"]),
                       undecided_honest_heights=tot("undecided"), split_rounds=tot("split_rounds"),
                       attacker_commits=tot("attacker_commits"),
                       attacker_commit_share=round(tot("attacker_commits") / tot("split_rounds"), 4)
                       if tot("split_rounds") else "",
                       double_signers=tot("double_signers"), caught=tot("caught"),
                       false_accusations=tot("false_accusations"),
                       extra_rounds_per_run=round(tot("extra_rounds") / len(cs), 2),
                       latency_p95_s=round(max(c["latency_p95_s"] for c in cs), 3),
                       runs_worse_than_honest=worse)
            for k in ("availability", "fresh_after", "ufls_events_after", "ufls_slots_after", "collapses",
                      "critical_served_share", "served_share"):
                m, ci = mean(k)
                row[k] = round(m, 4)
                if k in ("availability", "ufls_events_after", "served_share"):
                    row[k + "_ci95"] = round(ci, 4)
            row["passed"] = ok
            rows.append(row)
    return rows


def island_svg(rows, runs):
    labels, values, tips, rounds, ufls, avail, crit = [], [], [], [], [], [], []
    for r in rows:
        where = "超過 f" if r["b"] > 2 else ("無" if r["b"] == 0 else "f 以內")
        labels.append(f"{r['rule_label']}｜{r['b']} 個作惡（{where}）")
        share = r["attacker_commit_share"]
        values.append([r["fork_runs"] / r["runs"], share if share != "" else None])
        tips.append([f"{r['rule_label']}，{r['b']} 個作惡：{r['runs']} 次中有 {r['fork_runs']} 次分叉"
                     f"（{r['conflict_heights']} 個高度提交了兩個區塊）",
                     f"{r['rule_label']}，{r['b']} 個作惡：作惡提案者送出雙份區塊 {r['split_rounds']} 次，"
                     f"不含新報告的那份被提交 {r['attacker_commits']} 次" if share != "" else
                     f"{r['rule_label']}：沒有作惡節點"])
        rounds.append(f"{r['extra_rounds_per_run']:.0f}")
        ufls.append(f"{r['ufls_events_after']:.1f}")
        avail.append(f"{r['availability'] * 100:.1f}%")
        crit.append(f"{r['critical_served_share'] * 100:.1f}%")
    return matrix(labels, ["分叉的\n次數比例", "不含新報告的\n區塊被提交"], values, tips=tips, breaks=[3],
                  text_cols=[("每次多花\n的輪數", rounds), ("打擊後\n低頻卸載次數", ufls), ("收到新決策\n的時槽", avail),
                             ("關鍵饋線\n供電時間", crit)],
                  title="exp1 作惡節點在孤島上：f 以內也能讓舊狀態的決策過關，「報告必收」規則擋住",
                  subtitle=(f"C 台電 BFT（D 的程式相同）：7 個驗證者、12 條饋線，24 小時、凌晨約 3 點打擊後剩 45% 供電；"
                            f"每列 {runs} 次\n"
                            "作惡提案者的第二個區塊拿掉新的狀態報告、依舊狀態重算指令；規則：誠實節點對漏掉自己"
                            "已收到 60 秒以上報告的提案投 nil"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else f"{v * 100:.0f}%",
                  heat_header="比例", legend_label="比例",
                  desc="有無報告必收規則、不同作惡節點數下，分叉的比例、不含新報告的區塊被提交的比例、打擊後低頻卸載次數、"
                       "收到新決策的時槽比例和關鍵饋線供電時間")


# ---------------------------------------------------------------- main

def main():
    ap = common.common_args(seeds=5, runs=4)
    ap.add_argument("--part", choices=("consensus", "island", "all"), default="all")
    ap.add_argument("--heights", type=int, default=50, help="consensus part: heights per run")
    a = ap.parse_args()
    seeds, runs = (1, 2) if a.quick else (a.seeds, a.runs)
    heights = 12 if a.quick else a.heights
    out = a.out or os.path.join(common.ROOT, "results", "main", "exp1_equivocation")
    passed = True
    meta_base = {"step": "docs/DESIGN.md section 7, step 4", "seeds": seeds, "runs_per_seed": runs}

    if a.part in ("consensus", "all"):
        jobs = [(n, b, p, seed, runs, heights) for n in NS for b in range((n - 1) // 3 + 2) for p in PS
                for seed in range(seeds)]
        results = common.run_grid(consensus_job, jobs, a, out)
        if results is not None:
            rows = consensus_rows(results)
            print(f"{'n':>2} {'b':>2} {'p':>4} {'heights':>7} {'conflict':>8} {'forks':>5} {'undecided':>9} "
                  f"{'caught':>9} {'false':>5} {'p95 s':>6} {'round':>6}")
            for r in rows:
                print(f"{r['n']:>2} {r['b']:>2} {r['p']:>4} {r['heights']:>7} {r['conflict_heights']:>8} "
                      f"{r['fork_runs']:>5} {r['undecided_honest_heights']:>9} "
                      f"{str(r['caught']) + '/' + str(r['double_signers']):>9} {r['false_accusations']:>5} "
                      f"{r['latency_p95_s']:>6.2f} {r['mean_round']:>6}{'' if r['passed'] else '   FAIL'}")
            ok = all(r["passed"] for r in rows)
            passed = passed and ok
            common.write_results(out, rows, {
                "experiment": "exp1_equivocation", "part": "consensus", **meta_base, "heights_per_run": heights,
                "config": GridConfig(dispatch_period_s=SLOT_S).to_dict(), "committee_sizes": list(NS),
                "loss_p": list(PS), "compromised": "the last b validators (v{n-b} .. v{n-1})",
                "pass_rule": "b <= f: no conflicting honest commits and every honest validator commits every height; "
                             "conflicting proofs share >= f + 1 signers, all compromised when from the same round; "
                             "no honest validator accused; b = f + 1 at p = 0: conflicts happen",
                "all_passed": ok,
                "known_issues": [
                    "votes are sent once, not gossiped: a double signer is caught only when one honest validator "
                    "sees both of its votes, in practice when a commit proof reaches the other half by catch-up. "
                    "When the split makes the round fail without any commit (n = 7, b = 1: neither half reaches Q), "
                    "nobody sees both votes; CometBFT's vote gossip would show them (column caught)",
                    "beyond f, the attacker keeps splitting after a fork: the honest half whose chain the "
                    "compromised validators did not follow can never reach Q again (undecided heights); that is "
                    "the expected loss of liveness beyond f, not a bug",
                    "conflicting proofs from different rounds share >= f + 1 signers, but naming the culprits then "
                    "needs the votes of the rounds in between (locking), not just the two proofs",
                ]})
            with open(out + ".svg", "w", encoding="utf-8") as fh:
                fh.write(consensus_svg(rows, seeds * runs, heights))
            print("wrote", out + ".svg")
            common.drop_shards(out, a)

    if a.part in ("island", "all"):
        iout = out + "_island"
        jobs = [(b, rule, seed, run) for rule, _, _ in RULES for b in ISLAND_B
                for seed in range(seeds) for run in range(runs)]
        results = common.run_grid(island_job, jobs, a, iout)
        if results is not None:
            rows = island_rows(results)
            print(f"{'rule':<8} {'b':>2} {'forks':>5} {'undec':>5} {'split':>5} {'stale':>5} {'caught':>7} "
                  f"{'avail':>6} {'ufls>':>6} {'crit':>6} {'worse':>5}")
            for r in rows:
                print(f"{r['rule']:<8} {r['b']:>2} {r['fork_runs']:>5} {r['undecided_honest_heights']:>5} "
                      f"{r['split_rounds']:>5} {r['attacker_commits']:>5} "
                      f"{str(r['caught']) + '/' + str(r['double_signers']):>7} {r['availability']:>6.3f} "
                      f"{r['ufls_events_after']:>6.2f} {r['critical_served_share']:>6.3f} "
                      f"{r['runs_worse_than_honest']:>5}{'' if r['passed'] else '   FAIL'}")
            ok = all(r["passed"] for r in rows)
            passed = passed and ok
            common.write_results(iout, rows, {
                "experiment": "exp1_equivocation", "part": "island", **meta_base,
                "config": GridConfig(arch="C").to_dict(), "compromised": "v{7-b} .. v6",
                "rules": {k: {"label": lab, "report_inclusion_s": "inf" if math.isinf(s) else s} for k, lab, s in RULES},
                "pass_rule": "b <= 2: no conflicting commits, every honest validator commits every height; with the "
                             "rule also no attacker block committed and no run worse than the same run without "
                             "attack (UFLS after the strike, collapses, critical feeders); b = 2 without the rule: "
                             "attacker blocks committed",
                "all_passed": ok,
                "known_issues": [
                    "the attacker's second block is the one without the fresh state reports, the only other block "
                    "the honest validators accept; with the rule it is refused whenever it leaves out a report the "
                    "honest validators have held for 60 s, which here is always. A report less than 60 s old "
                    "(a feeder change just before a slot) may still be left out: one slot later it is old enough",
                    "beyond f (b = 3) the rule happens to stop this attacker as well, because a block's content "
                    "follows from inputs every honest validator holds; that is not a guarantee: the protocol "
                    "promises nothing beyond f",
                    "C only: D runs exactly the same code; the two differ in who runs the validators, which "
                    "decides how likely b compromised validators are (experiment 6), not what they can do",
                ]})
            with open(iout + ".svg", "w", encoding="utf-8") as fh:
                fh.write(island_svg(rows, seeds * runs))
            print("wrote", iout + ".svg")
            common.drop_shards(iout, a)

    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
