"""Experiment 6 (docs/DESIGN.md section 5): common-cause compromise, C against D.

C runs its 7 validators inside Taipower; D gives them to 7 organisations
(arch.d.ArchD.ORGS). The code is the same, so the questions are how likely an
attacker is to hold enough of them, and what each node gives away when it falls
(design doc questions 19 and 29). Four parts:

  keys      what an attacker holding b decision nodes can do, measured on a
            short island run (2 hours at a flat 45 % of the peak, the attack
            from minute 30 on):
              read   one of the nodes received every substation's feeder states,
                     that is the island's allocation (faults.Observe,
                     metrics.data_exposure)
              stall  no command is executed after the attack starts while the
                     honest run executes some: C's b validators stop voting
                     (faults.Offline); A and B's hacked centre keeps its
                     heartbeat and alive tokens but sends no command
                     (HackedCentre "freeze")
              forge  a command the honest run does not have is executed
                     ("restore_all": the forged commit proofs of
                     CompromisedValidators in C, HackedCentre in A and B)
            C for b = 0..7 (D runs the same code); A and B for b = 0 and 1
            (the active dispatch centre).
  exposure  per architecture and kind of node: how many substations' feeder
            states and commands it received, and whether it saw the island's
            measurements (supply and every feeder's demand). The nodes that see
            every substation's feeder states are the read surface.
  odds      the chance of each threshold in one period under the common-cause
            model (metrics.break_difficulty, theory.compromise_prob_events),
            with each architecture's own node-to-organisation map from its run
            and the thresholds the keys part measured, for A/B, C, D and D with
            two software implementations; checked against a Monte Carlo draw of
            the same model (theory.sample_compromised).
  boundary  the weak organisations' node risk at which D is as likely as C to
            lose k nodes (theory.break_even_weak_risk), against the chance that
            Taipower falls as a whole (shared accounts, staff, network), for
            k = 1 (read), f + 1 = 3 (stall) and Q = 5 (forge), with 2, 4 or 6 of
            D's 7 organisations weak. Below the line D is the safer design.

The risks are assumptions for one period of war (say a year): a strong
organisation's node 2 % (Taipower's sites, the telecom operator, the central
government), the shared software 1 % (C and D both run it, so it cancels out of
the boundary), and in the base case Taipower as a whole 5 % and a weak
organisation's node 10 % (the city emergency centre, the hospital, the water
utility, the fire department). They show trends, not real odds.

Checks pass when: in every seed the measured thresholds are read 1, stall 3,
forge 5 for C and 1, 1, 1 for A and B; A and B's substations receive only their
own commands while C and D's validators and substations receive every
substation's states and commands and the measurements; every Monte Carlo
estimate is within 4 standard errors of the closed form; and the boundary does
not depend on the shared software risk, rises with Taipower's risk, falls with
more weak organisations and rises from read to stall to forge.

Writes <out>.csv/.meta.json/.svg (boundary), <out>_keys.csv/.meta.json/.svg,
<out>_exposure.csv/.meta.json/.svg and <out>_odds.csv/.meta.json; exits with
status 1 if a check fails.

  python3 experiments/exp6_common_cause.py [--quick] [--seeds 3] [--workers 4] [--mc 200000]
      [--out results/main/exp6_common_cause]
"""
from __future__ import annotations

import math
import os
import random
import sys
from collections import defaultdict

import common
from gridq.arch.d import ArchD
from gridq.config import GridConfig
from gridq.events import EventKind
from gridq.faults import CompromisedValidators, HackedCentre, Observe, Offline
from gridq.metrics import break_difficulty, data_exposure, risk_model
from gridq.sim import simulate
from gridq.svgchart import chart, matrix
from gridq.theory import break_even_weak_risk, sample_compromised

V = [f"v{i}" for i in range(7)]
SHORT = dict(horizon_h=2.0, supply_jitter_h=0.0, supply_profile=((0.0, 0.45), (24.0, 0.45)))
START = 1800.0               # the attack starts at minute 30 (dispatch slot 6)
SETTLE_S = 60.0              # commands executed in the first minute were decided before the attack
SUBS = ("S1", "S2", "S3", "S4")

P_STRONG = 0.02              # a strong organisation's node falls on its own
P_WEAK = 0.10                # a weak organisation's node (base case)
P_ORG = 0.05                 # Taipower as a whole: every node it runs at once (base case)
P_SOFTWARE = 0.01            # a zero-day in the software every decision node runs
SOFTWARE_LEVELS = (0.005, 0.01, 0.02)
WEAK_ORGS = ("city_emergency", "hospital", "water_utility", "fire_department")
IMPLEMENTATIONS = (("v0", "v1", "v2", "v3"), ("v4", "v5", "v6"))     # D with two software implementations
P_ORGS = (0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10,      # x axis: Taipower's common
          0.125, 0.15, 0.175, 0.20, 0.25, 0.30)                                     # risk, finer where curves rise
N_WEAK = (2, 4, 6)
THRESHOLD_LABELS = (("read", "讀取"), ("stall", "癱瘓"), ("forge", "偽造"))
ROLES = (("centre", "調度中心（值班）"), ("backup", "備援中心"), ("validator", "驗證者"), ("substation", "變電所"))


def role_of(node: str) -> str:
    if node in ("centre", "backup"):
        return node
    return "validator" if node in V else "substation"


def accepted_after(sim, t0: float):
    return [(e.data["substation"], e.data["feeder"], e.data["target"], e.data.get("seq"))
            for e in sim.log.of_kind(EventKind.COMMAND_ACCEPTED) if e.t >= t0]


def reads_island(exposure, nodes) -> bool:
    """Did one of the nodes receive every substation's feeder states (the island's allocation)?"""
    return any(set(exposure.get(n, {}).get("states", ())) >= set(SUBS) for n in nodes)


def job(args):
    """One architecture, one seed: the honest run with faults.Observe, then each b's attacks."""
    arch, seed = args
    cfg = GridConfig(seed=seed, arch=arch, **SHORT)
    honest = simulate(cfg, Observe())
    exposure = data_exposure(honest.log)
    ref = set(accepted_after(honest, START))
    later = len(accepted_after(honest, START + SETTLE_S))
    rows = []
    for b in (range(8) if arch in "CD" else range(2)):
        nodes = V[7 - b:] if arch in "CD" else ["centre"][:b]
        row = dict(arch=arch, seed=seed, b=b, nodes=" ".join(nodes), honest_executed_later=later,
                   read=reads_island(exposure, nodes))
        if b == 0:
            row.update(executed_later=later, forged_executed=0, stall=False, forge=False)
        else:
            stall_f = Offline(nodes, start=START) if arch in "CD" else HackedCentre("freeze", start=START)
            forge_f = (CompromisedValidators(nodes, "restore_all", start=START) if arch in "CD"
                       else HackedCentre("restore_all", start=START))
            st, fo = simulate(cfg, stall_f), simulate(cfg, forge_f)
            executed_later = len(accepted_after(st, START + SETTLE_S))
            forged = [c for c in accepted_after(fo, START) if c not in ref]
            row.update(executed_later=executed_later, forged_executed=len(forged),
                       stall=later > 0 and executed_later == 0, forge=len(forged) > 0,
                       failovers=sum(1 for e in st.log.of_kind(EventKind.FAILOVER)
                                     if e.data.get("action") == "take_over"),
                       fallback_starts=sum(1 for e in st.log.of_kind(EventKind.FALLBACK_SCHEDULE)
                                           if e.data.get("action") == "start"))
        rows.append(row)
    return {"arch": arch, "seed": seed, "keys": rows, "exposure": exposure,
            "orgs": honest.log.run_meta["arch"]["orgs"]}


def thresholds_of(rows):
    """The smallest b that can read, stall and forge (None: no b in the table can)."""
    return {k: min((r["b"] for r in rows if r[k]), default=None) for k, _ in THRESHOLD_LABELS}


def odds_models(orgs_by_arch):
    """(key, label, orgs, node risk by organisation, Taipower risk by organisation, software events)."""
    taipower = {"taipower": P_STRONG}
    d_risk = {o: (P_WEAK if o in WEAK_ORGS else P_STRONG) for o in ArchD.ORGS}
    return (("AB", "A、B：調度中心＋備援（台電）", orgs_by_arch["A"], taipower, {"taipower": P_ORG}, [(P_SOFTWARE, None)]),
            ("C", "C：台電 7 個站點", orgs_by_arch["C"], taipower, {"taipower": P_ORG}, [(P_SOFTWARE, None)]),
            ("D", "D：7 個組織", orgs_by_arch["D"], d_risk, None, [(P_SOFTWARE, None)]),
            ("D2", "D：7 個組織，兩套軟體實作", orgs_by_arch["D"], d_risk, None,
             [(P_SOFTWARE, list(ns)) for ns in IMPLEMENTATIONS]))


def monte_carlo(orgs, node_risk, org_risk, software, thresholds, n, seed):
    _, events, p_nodes = risk_model(orgs, node_risk, org_risk, software)
    rng = random.Random(f"exp6-mc-{seed}")
    hits = {k: 0 for k in thresholds}
    for _ in range(n):
        c = len(sample_compromised(events, p_nodes, rng))
        for k, need in thresholds.items():
            hits[k] += c >= need
    return {k: h / n for k, h in hits.items()}


def boundary_rows(p_software=P_SOFTWARE):
    rows = []
    for k, name in ((1, "read"), (3, "stall"), (5, "forge")):
        for nw in N_WEAK:
            for p_org in P_ORGS:
                p_c = 1.0 - (1.0 - p_software) * (1.0 - p_org)
                be = break_even_weak_risk(k, p_c, P_STRONG, p_software, 7, nw)
                rows.append(dict(threshold=name, k=k, n_weak=nw, p_org=p_org, p_software=p_software,
                                 c_common=round(p_c, 6), break_even_weak=round(be, 6)))
    return rows


def boundary_checks(rows, others):
    curve = defaultdict(dict)
    for r in rows:
        curve[(r["k"], r["n_weak"])][r["p_org"]] = r["break_even_weak"]
    rising = all(all(c[b] >= c[a] - 1e-6 for a, b in zip(P_ORGS, P_ORGS[1:])) for c in curve.values())
    fewer_weak = all(curve[(k, 2)][x] >= curve[(k, 4)][x] - 1e-6 >= curve[(k, 6)][x] - 2e-6
                     for k in (1, 3, 5) for x in P_ORGS[1:])
    harder = all(curve[(1, nw)][x] <= curve[(3, nw)][x] + 1e-6 <= curve[(5, nw)][x] + 2e-6
                 for nw in N_WEAK for x in P_ORGS[1:])
    cancels = max(abs(a["break_even_weak"] - b["break_even_weak"]) for o in others for a, b in zip(rows, o))
    return {"rises_with_taipower_risk": rising, "falls_with_more_weak_orgs": fewer_weak,
            "rises_from_read_to_forge": harder, "software_risk_cancels": cancels < 1e-6,
            "software_max_difference": cancels}


# ---------------------------------------------------------------- charts
def pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def boundary_svg(rows):
    panels = []
    for k, title in ((1, "讀取：攻破 1 個節點"), (3, "癱瘓：攻破 3 個（f + 1）"), (5, "偽造：攻破 5 個（Q）")):
        series = []
        for slot, nw in ((1, 2), (2, 4), (3, 6)):
            pts = [(r["p_org"], r["break_even_weak"],
                    f"台電整體被攻破 {r['p_org'] * 100:.0f}%、{nw} 個弱組織：弱組織節點低於 "
                    f"{r['break_even_weak'] * 100:.1f}% 時，D 被攻破 {k} 個節點的機率比 C 低")
                   for r in rows if r["k"] == k and r["n_weak"] == nw]
            series.append({"label": f"{nw} 個弱組織", "slot": slot, "points": pts,
                           "band": [(x, P_STRONG, y) for x, y, _ in pts] if nw == 4 else None,
                           "end_label": f"{nw} 個弱組織"})
        panels.append({"title": title, "series": series,
                       "refs": [{"label": "弱組織和台電站點一樣強（2%）", "points": [(0.0, P_STRONG), (P_ORGS[-1], P_STRONG)],
                                 "label_at": "none"}],
                       "extra": [{"x": P_ORG, "y": P_WEAK, "slot": 2,
                                  "tip": f"基準情境：台電整體 {P_ORG * 100:.0f}%、弱組織節點 {P_WEAK * 100:.0f}%（4 個弱組織）"}]})
    return chart(panels, title="exp6 共因失效：弱組織要多弱，D（7 個組織）才不如 C（台電 7 站）",
                 subtitle=("線以下是 D 比較安全的區域：弱組織節點被攻破的機率低於這條線，D 被攻下 k 個節點的機率就比 C 低\n"
                           "假設（一段戰時期間）：台電各站點、電信、中央政府的節點各自 2%；共用軟體的漏洞兩邊都有，比較時抵銷\n"
                           "菱形是示意的基準情境：台電整體被攻破 5%、4 個弱組織的節點各 10%"),
                 legend=[("2 個弱組織", 1, "line"), ("4 個弱組織", 2, "line"), ("6 個弱組織", 3, "line"),
                         ("D 比較安全（4 個弱組織）", 2, "band"), ("弱組織和台電一樣強", 0, "ref")],
                 x_label="台電整體一次被攻破的機率（C 的共因）", y_label="弱組織節點被攻破的機率",
                 x_domain=(0.0, P_ORGS[-1]), y_domain=(0.0, 1.0), x_ticks=[0.0, 0.1, 0.2, 0.3],
                 x_fmt=pct, y_fmt=pct, panel_w=270, plot_h=250,
                 desc="三個面板分別是讀取、癱瘓、偽造所需的節點數；每條線是 D 和 C 一樣容易被攻破的弱組織風險，線以下 D 比較安全")


def keys_svg(rows):
    labels, values, tips = [], [], []
    for r in rows:
        who = (f"A、B｜攻破 {r['b']} 個調度中心" if r["arch"] == "AB" else f"C、D｜攻破 {r['b']} 個驗證者")
        labels.append(who)
        values.append([1.0 if r[k] else 0.0 for k, _ in THRESHOLD_LABELS])
        tips.append([f"{who}：{lab}{'做得到' if r[k] else '做不到'}（{r['runs']} 次執行都一樣）"
                     for k, lab in THRESHOLD_LABELS])
    return matrix(labels, ["讀取\n全島資料", "癱瘓\n調度", "偽造\n指令"], values, tips=tips, breaks=[1],
                  title="exp6 攻破幾個節點能做什麼：A、B 一個就全拿，C、D 是 1、3、5",
                  subtitle=("2 小時、供電 45% 的孤島，第 30 分鐘起攻擊；讀取：收到每座變電所的饋線狀態；"
                            "癱瘓：之後沒有任何指令被執行\n"
                            "偽造：執行了誠實執行沒有的指令（全部復電）；C、D 是同一份程式，"
                            "被攻破的驗證者停止投票（癱瘓）或自己簽提交證明（偽造）"),
                  value_fmt=lambda v: "做得到" if v >= 0.5 else "做不到", ramp=False,
                  desc="各架構被攻破不同節點數時，攻擊者能否讀取全島資料、癱瘓調度、偽造指令")


def exposure_svg(rows):
    labels, values, tips, count, meas, whole = [], [], [], [], [], []
    for r in rows:
        labels.append(f"{r['arch']}｜{r['role_label']}")
        values.append([r["states_seen"] / len(SUBS), r["commands_seen"] / len(SUBS)])
        tips.append([f"{r['arch']} 的{r['role_label']}：收到 {r['states_seen']} 座變電所的饋線狀態",
                     f"{r['arch']} 的{r['role_label']}：收到 {r['commands_seen']} 座變電所的指令"])
        count.append(str(r["nodes"]))
        meas.append("有" if r["measurements"] else "—")
        whole.append("是" if r["reads_island"] else "否")
    breaks = [i for i in range(len(rows) - 1) if rows[i]["arch"] != rows[i + 1]["arch"]]
    surface = {r["arch"]: r["read_surface"] for r in rows}
    return matrix(labels, ["收到的變電所\n饋線狀態", "收到的變電所\n指令"], values, tips=tips, breaks=breaks,
                  text_cols=[("節點數", count), ("全島供電\n與需求", meas), ("攻破一個就讀到\n全島供電分配", whole)],
                  title="exp6 攻破一個節點看得到什麼：C、D 的讀取面比 A、B 大",
                  subtitle=(f"2 小時、供電 45% 的誠實執行，記錄每個節點實際收到的狀態回報、指令和量測（4 座變電所）\n"
                            f"收到每座變電所饋線狀態（全島哪裡有電）的節點：A {surface['A']} 個、B {surface['B']} 個、"
                            f"C {surface['C']} 個、D {surface['D']} 個\n"
                            "C、D 多出來的是 7 個驗證者和 4 座變電所：提交證明簽的是整個區塊，所以變電所收到整個區塊"),
                  value_fmt=lambda v: f"{v * 100:.0f}%", heat_header="比例", legend_label="比例",
                  desc="各架構每種節點收到幾座變電所的狀態和指令、有沒有全島量測、攻破一個是否就讀到全島")


# ---------------------------------------------------------------- main
def main():
    ap = common.common_args(seeds=3, runs=1)
    ap.add_argument("--mc", type=int, default=200_000, help="Monte Carlo draws per architecture")
    a = ap.parse_args()
    seeds = 1 if a.quick else a.seeds
    n_mc = 20_000 if a.quick else a.mc
    out = a.out or os.path.join(common.ROOT, "results", "main", "exp6_common_cause")
    jobs = [(arch, seed) for seed in range(seeds) for arch in "ABCD"]
    results = common.run_grid(job, jobs, a, out)
    if results is None:
        return
    checks = {}

    # keys: C stands for C and D (one code path); A and B side by side, then merged when equal
    keys = defaultdict(list)
    for res in results:
        for r in res["keys"]:
            keys[(res["arch"], r["b"])].append(r)
    measured = {arch: thresholds_of([r for (ar, _), rs in keys.items() if ar == arch for r in rs])
                for arch in "ABCD"}
    per_seed = {(res["arch"], res["seed"]): thresholds_of(res["keys"]) for res in results}
    want = {"A": {"read": 1, "stall": 1, "forge": 1}, "B": {"read": 1, "stall": 1, "forge": 1},
            "C": {"read": 1, "stall": 3, "forge": 5}, "D": {"read": 1, "stall": 3, "forge": 5}}
    checks["thresholds"] = all(per_seed[(arch, s)] == want[arch] for arch in "ABCD" for s in range(seeds))
    checks["a_b_no_failover_or_table_under_freeze"] = all(
        r.get("failovers", 0) == 0 and r.get("fallback_starts", 0) == 0 for (ar, _), rs in keys.items() if ar in "AB"
        for r in rs)
    keys_rows = []
    for arch, label in (("AB", None), ("C", None)):
        for b in (range(2) if arch == "AB" else range(8)):
            rs = keys[("A", b)] + keys[("B", b)] if arch == "AB" else keys[("C", b)] + keys[("D", b)]
            same = all((r["read"], r["stall"], r["forge"]) == (rs[0]["read"], rs[0]["stall"], rs[0]["forge"]) for r in rs)
            keys_rows.append(dict(arch=arch, b=b, seeds=seeds, runs=len(rs), read=rs[0]["read"], stall=rs[0]["stall"],
                                  forge=rs[0]["forge"], same_in_every_run=same,
                                  executed_after_attack=round(sum(r["executed_later"] for r in rs) / len(rs), 1),
                                  honest_executed_after=round(sum(r["honest_executed_later"] for r in rs) / len(rs), 1),
                                  forged_executed=round(sum(r["forged_executed"] for r in rs) / len(rs), 1)))
    checks["keys_same_in_every_run"] = all(r["same_in_every_run"] for r in keys_rows)

    # exposure, from seed 0 (checked equal in every seed)
    by_arch = {res["arch"]: res for res in results if res["seed"] == 0}
    checks["exposure_same_in_every_seed"] = all(res["exposure"] == by_arch[res["arch"]]["exposure"] for res in results)
    exposure_rows = []
    for arch in "ABCD":
        ex = by_arch[arch]["exposure"]
        surface = sum(1 for n in ex if reads_island(ex, [n]))
        for role, role_label in ROLES:
            ns = [n for n in ex if role_of(n) == role]
            if not ns:
                continue
            states = sorted({len(ex[n]["states"]) for n in ns})
            cmds = sorted({len(ex[n]["commands"]) for n in ns})
            meas = {ex[n]["measurements"] for n in ns}
            if arch == "D" and role == "validator":
                role_label = "驗證者（7 個組織）"
            exposure_rows.append(dict(arch=arch, role=role, role_label=role_label, nodes=len(ns),
                                      states_seen=states[0], commands_seen=cmds[0], measurements=all(meas),
                                      uniform=len(states) == len(cmds) == len(meas) == 1,
                                      reads_island=reads_island(ex, ns[:1]), read_surface=surface))
    role_row = {(r["arch"], r["role"]): r for r in exposure_rows}
    checks["exposure"] = (all(r["uniform"] for r in exposure_rows)
                          and all((role_row[(a_, "substation")]["states_seen"], role_row[(a_, "substation")]["commands_seen"],
                                   role_row[(a_, "substation")]["measurements"]) == (1, 1, False) for a_ in "AB")
                          and all((r["states_seen"], r["commands_seen"], r["measurements"]) == (len(SUBS), len(SUBS), True)
                                  for r in exposure_rows if r["arch"] in "CD")
                          and role_row[("A", "centre")]["reads_island"] and role_row[("A", "backup")]["states_seen"] == 4)

    # odds at the base case, thresholds as measured
    orgs_by_arch = {arch: by_arch[arch]["orgs"] for arch in "ABCD"}
    odds_rows = []
    mc_ok = True
    for key, label, orgs, node_risk, org_risk, software in odds_models(orgs_by_arch):
        th = measured["A" if key == "AB" else ("C" if key == "C" else "D")]
        exact = break_difficulty(orgs, th, node_risk, org_risk, software)
        mc = monte_carlo(orgs, node_risk, org_risk, software, th, n_mc, key)
        row = dict(arch=key, label=label, nodes=len(orgs), organisations=len(set(orgs.values())), mc_draws=n_mc)
        for k, _ in THRESHOLD_LABELS:
            se = math.sqrt(max(exact[k] * (1.0 - exact[k]), 1e-12) / n_mc)
            z = (mc[k] - exact[k]) / se
            mc_ok &= abs(z) <= 4.0
            row.update({f"{k}_nodes": th[k], f"{k}_prob": round(exact[k], 6), f"{k}_mc": round(mc[k], 6),
                        f"{k}_z": round(z, 2)})
        odds_rows.append(row)
    checks["monte_carlo_matches_closed_form"] = mc_ok

    # boundary
    rows = boundary_rows()
    others = [boundary_rows(p) for p in SOFTWARE_LEVELS if p != P_SOFTWARE]
    bchecks = boundary_checks(rows, others)
    checks.update({k: v for k, v in bchecks.items() if isinstance(v, bool)})
    passed = all(checks.values())

    print("what b nodes can do (read / stall / forge):")
    for r in keys_rows:
        print(f"  {r['arch']:<2} b={r['b']}  {'R' if r['read'] else '-'} {'S' if r['stall'] else '-'} "
              f"{'F' if r['forge'] else '-'}   executed after attack {r['executed_after_attack']:>5} "
              f"(honest {r['honest_executed_after']}), forged {r['forged_executed']}")
    print("exposure:")
    for r in exposure_rows:
        print(f"  {r['arch']} {r['role']:<10} x{r['nodes']}  states {r['states_seen']}/4  commands {r['commands_seen']}/4  "
              f"measurements {r['measurements']}  read surface {r['read_surface']}")
    print("odds (base case):")
    for r in odds_rows:
        print(f"  {r['arch']:<3} " + "  ".join(f"{k}>={r[k + '_nodes']}: {r[k + '_prob']:.4f} (mc {r[k + '_mc']:.4f}, "
                                               f"z {r[k + '_z']:+.1f})" for k, _ in THRESHOLD_LABELS))
    print("boundary, 4 weak organisations:")
    for k in (1, 3, 5):
        print(f"  k={k} " + " ".join(f"{r['p_org']:.2f}:{r['break_even_weak']:.3f}" for r in rows
                                    if r["k"] == k and r["n_weak"] == 4 and r["p_org"] in (0.0, 0.01, 0.05, 0.1, 0.2, 0.3)))
    for k, v in checks.items():
        print(f"  check {k}: {'ok' if v else 'FAIL'}")

    assumptions = {"strong_node": P_STRONG, "weak_node_base": P_WEAK, "taipower_common_base": P_ORG,
                   "software": P_SOFTWARE, "software_levels_checked": list(SOFTWARE_LEVELS),
                   "weak_orgs": list(WEAK_ORGS), "implementations": [list(x) for x in IMPLEMENTATIONS],
                   "period": "one period of war, say a year; the numbers are assumptions that show trends"}
    common_meta = {"step": "docs/DESIGN.md section 7, step 4", "seeds": seeds, "island": SHORT,
                   "attack_start_s": START, "assumptions": assumptions, "checks": checks, "all_passed": passed}
    common.write_results(out, rows, {
        "experiment": "exp6_common_cause (boundary)", **common_meta,
        "p_org_grid": list(P_ORGS), "n_weak": list(N_WEAK), "boundary_checks": bchecks,
        "pass_rule": "the boundary does not depend on the shared software risk (it hits C and D alike), rises "
                     "with Taipower's common risk, falls with more weak organisations, rises from read to "
                     "stall to forge",
        "known_issues": [
            "the risks are assumptions; only their relations (common versus independent, strong versus weak) "
            "carry over to a real deployment",
            "break_even_weak = p_strong where C and D are equal with weak = strong (Taipower's common risk 0): "
            "D is never the safer design there",
            "events are independent: a campaign against several weak organisations at once (correlated breaches) "
            "would raise D's risks; so would a shared supplier or shared staff between organisations"]})
    common.write_results(out + "_keys", keys_rows, {
        "experiment": "exp6_common_cause (what b nodes can do)", **common_meta, "thresholds": measured,
        "pass_rule": "read 1, stall 3, forge 5 for C (= D) and 1, 1, 1 for A and B in every seed",
        "known_issues": [
            "D runs C's code, so its rows are C's; only the ownership differs",
            "stall in C, D: the compromised validators stop voting (as if offline); in A, B the hacked centre "
            "keeps its heartbeat and alive tokens, so neither the backup nor B's table takes over"]})
    common.write_results(out + "_exposure", exposure_rows, {
        "experiment": "exp6_common_cause (what each node receives)", **common_meta,
        "read_surface": "the nodes that received every substation's feeder states (the island's allocation)",
        "pass_rule": "A, B substations receive only their own commands and state; C, D validators and "
                     "substations receive every substation's states and commands and the measurements",
        "known_issues": [
            "C and D's substations receive whole blocks because the commit proof signs the whole block; a "
            "Merkle root of the commands in the header would let each substation get only its own commands "
            "with a proof (future work), shrinking C and D's read surface to the 7 validators",
            "A and B's standby centre receives every state report but no measurements until it takes over",
            "what is visible is not hidden by encryption here: confidential BFT (secret sharing) and "
            "data minimisation are the design doc's answers to question 29"]})
    common.write_results(out + "_odds", odds_rows, {
        "experiment": "exp6_common_cause (odds at the base case)", **common_meta, "mc_draws": n_mc,
        "pass_rule": "every Monte Carlo estimate within 4 standard errors of the closed form",
        "known_issues": ["A and B's thresholds are all 1: one hacked centre reads, stalls (freeze) and forges"]})
    for path, svg in ((out + ".svg", boundary_svg(rows)), (out + "_keys.svg", keys_svg(keys_rows)),
                      (out + "_exposure.svg", exposure_svg(exposure_rows))):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print("wrote", path)
    common.drop_shards(out, a)
    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
