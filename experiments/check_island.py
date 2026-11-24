"""Step 3 check (docs/DESIGN.md section 7): the four architectures on one island with the power model.

One island event of 24 hours: 12 feeders at 4 substations (loads.default_island),
supply G(t) from cfg.supply_profile (70 % of the peak demand, a strike near hour 3,
then recovery; every point shifted by one random draw of up to an hour per run).
The same policy (policy.decide) runs in every architecture on the same loads,
supply and network draws. Scenarios, all with the strike:

  honest          no site lost
  one_site        the strike also destroys one site: the dispatch centre (A, B)
                  or one validator (C, D)
  two_sites       two sites: both centres (A, B) or two validators (C, D); the
                  supply falls to 45 %, above the 40 % B's table assumes
  two_sites_deep  as two_sites, but the supply falls to 25 % and stays there
                  (question 22: the table assumes 40 %, the island has 25 %)

Checks pass when:
  * honest: A, B, C and D make the same decisions (the same commands in the
    same slots, in every run), every command is executed, every substation
    gets a fresh decision in every slot, nothing collapses;
  * one_site: A and B fail over to the backup and C and D keep deciding, with
    at most one slot of decisions missed; C and D decide exactly as without
    the loss;
  * two_sites: B's substations run the table and it causes no UFLS; C and D
    decide exactly as without the loss (f = 2 validators lost) and cause no
    UFLS after the strike;
  * two_sites_deep: in every run B's table trips UFLS again after the strike,
    and C and D never do (the strike itself trips UFLS everywhere: no dispatch
    reacts within the 2 s UFLS delay);
  * no island collapses anywhere (the critical feeders fit in 25 % of the peak).

Sensitivity (the sweep): two sites lost at a strike after which the supply
stays at a share s of the peak, s from 20 % to 50 % (two_sites_deep is the
point s = 25 %). Checks pass when C and D never trip UFLS after the strike and
never collapse, at any s, and B's table trips UFLS again in every run whenever
s is below the 40 % it was planned for.

Writes <out>.csv (one row per scenario and architecture, then per sweep point
and architecture), <out>.meta.json, <out>.svg (the scenarios) and
<out>_sweep.svg, and exits with status 1 if a check fails.

  python3 experiments/check_island.py [--quick] [--seeds 3] [--runs 4] [--workers 4] [--out results/main/check_island]
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import common
from gridq.config import GridConfig
from gridq.events import EventKind
from gridq.faults import Offline
from gridq.hashing import canonical_hash
from gridq.loads import Loads, default_island
from gridq.metrics import island_metrics, mean_ci95
from gridq.power import supply_scenario
from gridq.sim import Simulator
from gridq.svgchart import chart, matrix

ARCHES = (("A", "A 中央＋備援"), ("B", "B 中央＋在地檢查"), ("C", "C 台電 BFT"), ("D", "D 多組織 BFT"))
TWO = ("centre", "backup", "v5", "v6")
SWEEP = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)   # supply share after the strike (two sites lost)
DEEP_SHARE = 0.25


def flat_after_strike(share: float) -> tuple:
    """70 % of the peak until the strike near hour 3, then `share` for the rest of the day."""
    return ((0.0, 0.70), (3.0, 0.70), (3.0, share), (24.0, share))


SCENARIOS = (        # key, label, short label, supply profile (None: the default), nodes lost at the strike
    ("honest", "無故障", "無故障", None, ()),
    ("one_site", "打擊同時失去一個站點", "失去一站", None, ("centre", "v6")),
    ("two_sites", "失去兩個站點，供電 45%（高於備案假設的 40%）", "失去兩站", None, TWO),
    ("two_sites_deep", "失去兩個站點，供電只剩 25%（問題 22）", "兩站＋25%", flat_after_strike(DEEP_SHARE), TWO),
)
AFTER_S = 60.0       # UFLS later than this after the strike is not the strike's own trip


def strike_time(supply) -> float:
    """The first instant drop of G(t): two points at one time, the second lower."""
    for (t0, v0), (t1, v1) in zip(supply.points, supply.points[1:]):
        if t0 == t1 and v1 < v0:
            return t0
    raise ValueError("the supply scenario has no strike")


def after_strike(log, cfg, strike: float, n_subs: int) -> dict:
    """UFLS and fresh decisions after the strike's dispatch slot."""
    period, n = cfg.dispatch_period_s, cfg.n_dispatches
    k0 = int(strike // period)
    later = range(k0 + 1, n)
    ufls_t = sorted({e.t for e in log.of_kind(EventKind.UFLS_TRIP) if e.t > strike + AFTER_S})
    ufls_slots = {int(t // period) for t in ufls_t}
    fresh = {(e.data.get("substation"), e.data.get("slot")) for e in log.of_kind(EventKind.DECISION_APPLIED)
             if type(e.data.get("slot")) is int and e.data["slot"] > k0 and e.t < (e.data["slot"] + 1) * period}
    return {"ufls_events_after": len(ufls_t),
            "ufls_slots_after": round(len(ufls_slots & set(later)) / len(later), 6) if later else 0.0,
            "fresh_after": round(len(fresh) / (n_subs * len(later)), 6) if later else 1.0}


def job(args):
    """One island event in one scenario (or sweep point: key "sweep" and a supply share), all four architectures."""
    seed, run, key, share = args
    if key == "sweep":
        profile, lost = flat_after_strike(share), TWO
    else:
        _, _, _, profile, lost = next(s for s in SCENARIOS if s[0] == key)
    base = GridConfig(seed=seed) if profile is None else GridConfig(seed=seed, supply_profile=profile)
    specs = default_island()
    peak = Loads(base, specs, run).peak_kw()
    supply = supply_scenario(base, peak, run)
    strike = strike_time(supply)
    out = []
    for arch, _ in ARCHES:
        cfg = base.with_(arch=arch)
        sim = Simulator(cfg, Offline(lost, start=strike) if lost else None, run, specs=specs, supply=supply).run()
        m = island_metrics(sim.log)
        m.update(after_strike(sim.log, cfg, strike, len(sim.link.subs)))
        m["decisions"] = canonical_hash([[e.data["expires"], e.data["substation"], e.data["feeder"], e.data["target"]]
                                         for e in sim.log.of_kind(EventKind.COMMAND_ISSUED)])
        out.append(dict(scenario=key, share=share, arch=arch, seed=seed, run=run, strike_h=round(strike / 3600.0, 3),
                        **m))
    return out


def cells_of(cells, key):
    return [c for (k, _), cs in cells.items() if k == key for c in cs]


def summarize(results):
    cells, sweep = defaultdict(list), defaultdict(list)
    for res in results:
        for c in res:
            if c["scenario"] == "sweep":
                sweep[(c["share"], c["arch"])].append(c)
                if c["share"] == DEEP_SHARE:
                    cells[("two_sites_deep", c["arch"])].append(dict(c, scenario="two_sites_deep"))
            else:
                cells[(c["scenario"], c["arch"])].append(c)
    # each architecture's decisions without a fault, run by run (commands and their slots)
    honest = {(c["arch"], c["seed"], c["run"]): c["decisions"] for c in cells_of(cells, "honest")}
    same_decisions = all(len({honest[(a, sd, r)] for a, _ in ARCHES}) == 1 for _, sd, r in honest)
    rows = []
    for key, label, _, profile, _ in SCENARIOS:
        for arch, arch_label in ARCHES:
            cs = cells[(key, arch)]
            n_slots = GridConfig().n_dispatches
            every = lambda pred: all(pred(c) for c in cs)  # noqa: E731
            same = lambda c: c["decisions"] == honest[(c["arch"], c["seed"], c["run"])]  # noqa: E731
            ok = every(lambda c: c["collapses"] == 0 and c["critical_served_share"] == 1.0)
            if key == "honest":
                ok = ok and same_decisions and every(lambda c: c["issued"] == c["executed"] and c["availability"] == 1.0)
            elif key == "one_site":
                ok = ok and every(lambda c: c["availability"] >= 1.0 - 1.0 / n_slots)
                if arch in "AB":
                    ok = ok and every(lambda c: c["failovers"] >= 1)
                if arch in "CD":
                    ok = ok and every(same)
            elif key == "two_sites":
                if arch == "B":
                    ok = ok and every(lambda c: c["fallback_starts"] == 4 and c["ufls_events_after"] == 0)
                if arch in "CD":
                    ok = ok and every(lambda c: c["availability"] == 1.0 and c["ufls_events_after"] == 0) and every(same)
            elif key == "two_sites_deep":
                if arch == "B":
                    ok = ok and every(lambda c: c["ufls_events_after"] > 0)
                if arch in "CD":
                    ok = ok and every(lambda c: c["availability"] == 1.0 and c["ufls_events_after"] == 0)
            mean = lambda k: mean_ci95([float(c[k]) for c in cs])  # noqa: E731
            row = dict(scenario=key, scenario_label=label, arch=arch, arch_label=arch_label, runs=len(cs),
                       same_decisions_as_honest=round(sum(map(same, cs)) / len(cs), 4) if profile is None else "")
            for k in ("critical_served_share", "served_share", "fairness", "availability", "fresh_after",
                      "ufls_slots_after", "ufls_events_after", "ufls_events", "ufls_trips", "collapses", "dark_h",
                      "failovers", "fallback_starts", "table_switches", "issued", "executed"):
                m, ci = mean(k)
                row[k] = round(m, 4)
                if k in ("served_share", "fairness", "availability", "ufls_events_after", "ufls_slots_after"):
                    row[k + "_ci95"] = round(ci, 4)
            rej = defaultdict(int)
            for c in cs:
                for k, v in c.items():
                    if k.startswith("rejected:"):
                        rej[k] += v
            row["rejected"] = ";".join(f"{k[9:]}={v}" for k, v in sorted(rej.items())) or "-"
            row["passed"] = ok
            rows.append(row)
    for share in SWEEP:
        for arch, arch_label in ARCHES:
            cs = sweep[(share, arch)]
            every = lambda pred: all(pred(c) for c in cs)  # noqa: E731
            if arch in "CD":
                ok = every(lambda c: c["collapses"] == 0 and c["ufls_events_after"] == 0)
            elif arch == "B" and share < GridConfig().plan_supply_share - 1e-9:
                ok = every(lambda c: c["ufls_events_after"] > 0)
            else:
                ok = True
            row = dict(scenario="sweep", scenario_label=f"失去兩個站點，打擊後供電 {share:.0%}", arch=arch,
                       arch_label=arch_label, runs=len(cs), supply_share=share)
            for k in ("critical_served_share", "served_share", "fairness", "availability", "fresh_after",
                      "ufls_slots_after", "ufls_events_after", "collapses", "dark_h", "table_switches"):
                m, ci = mean_ci95([float(c[k]) for c in cs])
                row[k], row[k + "_ci95"] = round(m, 4), round(ci, 4)
            row["passed"] = ok
            rows.append(row)
    return rows


def svg(rows, runs):
    by = {(r["scenario"], r["arch"]): r for r in rows if r["scenario"] != "sweep"}
    labels, values, tips, served, fair, table = [], [], [], [], [], []
    for key, label, short, _, _ in SCENARIOS:
        for arch, arch_label in ARCHES:
            r = by[(key, arch)]
            labels.append(f"{short}｜{arch_label}")
            values.append([r["ufls_slots_after"], r["fresh_after"]])
            tips.append([f"{label}，{arch_label}：打擊後的調度時槽有 {r['ufls_slots_after'] * 100:.0f}% 發生低頻卸載，"
                         f"每次平均 {r['ufls_events_after']:.1f} 次",
                         f"{label}，{arch_label}：打擊後 {r['fresh_after'] * 100:.0f}% 的（變電所, 時槽）"
                         f"在時槽內收到驗證過的新決策"])
            served.append(f"{r['served_share'] * 100:.1f}%")
            fair.append(f"{r['fairness']:.2f}")
            table.append(f"{r['table_switches']:.0f}" if arch == "B" else "—")
    return matrix(labels, ["打擊後有低頻卸載\n的調度時槽", "打擊後收到\n新決策的時槽"], values, tips=tips,
                  text_cols=[("用戶用電\n滿足率", served), ("非關鍵饋線\n輪停公平性", fair), ("預簽輪停表\n切換次數", table)],
                  breaks=[3, 7, 11],
                  title="第 3 步檢查：同一套政策，四種架構，打擊後誰能重新分配",
                  subtitle=(f"12 條饋線、4 座變電所，24 小時、{runs} 次；凌晨約 3 點打擊，供電掉到 45% 或 25%\n"
                            "打擊同時失去站點：A、B 的調度中心，C、D 的驗證者；B 失去兩個中心後跑預簽輪停表（假設還有 40% 的電）\n"
                            "打擊當下的低頻卸載各架構都有，不列入；表內是各次的平均"),
                  value_fmt=lambda v: "<1%" if 0 < v < 0.005 else ">99%" if 0.995 <= v < 1 else f"{v * 100:.0f}%",
                  heat_header="調度時槽的比例", legend_label="時槽的比例",
                  desc="各情境與架構在打擊後，發生低頻卸載的調度時槽比例、變電所收到新決策的比例、用戶用電滿足率、"
                       "輪停公平性和預簽輪停表的切換次數")


def svg_sweep(rows, runs):
    """Three small multiples on one 0-100 % axis against the supply share after the strike."""
    sw = [r for r in rows if r["scenario"] == "sweep"]
    pct = lambda v: f"{v * 100:.0f}%"  # noqa: E731
    plan = GridConfig().plan_supply_share
    lines = (("A", 1, "A 中央＋備援"), ("B", 2, "B 中央＋在地檢查"), ("C", 3, "C、D 委員會（兩者相同）"))
    panels = []
    for key, title, value in (("ufls_slots_after", "打擊後有低頻卸載的調度時槽", lambda r: r["ufls_slots_after"]),
                              ("dark_h", "孤島全黑的時間", lambda r: r["dark_h"] / 24.0),
                              ("critical_served_share", "關鍵饋線的供電時間", lambda r: r["critical_served_share"])):
        series = []
        for arch, slot, label in lines:
            rs = sorted((r for r in sw if r["arch"] == arch), key=lambda r: r["supply_share"])
            ci = (lambda r: r[key + "_ci95"] / 24.0) if key == "dark_h" else (lambda r: r[key + "_ci95"])
            series.append({"label": label, "slot": slot,
                           "points": [(r["supply_share"], value(r),
                                       f"{label}，打擊後供電 {pct(r['supply_share'])}：{title} {pct(value(r))}；"
                                       f"低頻卸載每次 {r['ufls_events_after']:.1f} 次、全黑 {r['dark_h']:.1f} 小時、"
                                       f"用戶用電滿足率 {pct(r['served_share'])}") for r in rs],
                           "band": [(r["supply_share"], max(0.0, value(r) - ci(r)), min(1.0, value(r) + ci(r)))
                                    for r in rs]})
        panels.append({"title": title, "series": series,
                       "refs": [{"label": f"B 的預簽輪停表假設的供電（{pct(plan)}）", "points": [(plan, 0.0), (plan, 1.0)],
                                 "label_at": "none"}]})
    legend = [(label, slot, "line") for _, slot, label in lines] + [(f"B 的預簽輪停表假設的供電（{pct(plan)}）", 0, "ref")]
    return chart(panels, title="打擊後只剩多少電：固定備案在低於假設時超載，委員會重新分配",
                 subtitle=(f"失去兩個站點（A、B 兩個調度中心都沒了，C、D 少兩個驗證者），打擊後供電固定在峰值的某個比例；"
                           f"每點 {runs} 次、24 小時，帶狀為 95% 信賴區間\n"
                           "20% 時傍晚的關鍵負載超過供電：A 只能維持現狀、B 的規則不准切關鍵饋線，低頻卸載切不到它們，孤島全黑"
                           "（右圖 20% 處 A 與 B 的點重疊）"),
                 legend=legend, x_label="打擊後的供電（峰值需求的比例）", y_label="比例",
                 x_domain=(min(SWEEP), max(SWEEP)), x_ticks=list(SWEEP), x_fmt=pct, y_domain=(0.0, 1.0), y_fmt=pct,
                 panel_w=300, desc="失去兩個站點、打擊後供電從 20% 到 50% 時，各架構的低頻卸載時槽比例、全黑時間和關鍵饋線供電時間")


def main():
    ap = common.common_args(seeds=3, runs=4)
    a = ap.parse_args()
    seeds, runs = (1, 2) if a.quick else (a.seeds, a.runs)
    out = a.out or os.path.join(common.ROOT, "results", "main", "check_island")
    jobs = [(seed, run, key, None) for key, *_ in SCENARIOS if key != "two_sites_deep"
            for seed in range(seeds) for run in range(runs)]
    jobs += [(seed, run, "sweep", share) for share in SWEEP for seed in range(seeds) for run in range(runs)]
    rows = summarize(common.run_jobs(job, jobs, a.workers))

    print(f"{'scenario':<15} {'arch':<4} {'served':>7} {'fair':>5} {'avail':>6} {'ufls>':>6} {'slots':>6} "
          f"{'collapse':>8} {'failover':>8} {'table':>6}")
    for r in rows:
        name = r["scenario"] if r["scenario"] != "sweep" else f"sweep {r['supply_share']:.0%}"
        print(f"{name:<15} {r['arch']:<4} {r['served_share']:>7.1%} {r['fairness']:>5.2f} "
              f"{r['availability']:>6.1%} {r['ufls_events_after']:>6.1f} {r['ufls_slots_after']:>6.1%} "
              f"{r['collapses']:>8.1f} {r.get('failovers', 0):>8.1f} {r['table_switches']:>6.0f}"
              f"{'' if r['passed'] else '   FAIL'}")
    passed = all(r["passed"] for r in rows)
    cfg = GridConfig()
    meta = {
        "experiment": "check_island", "step": "docs/DESIGN.md section 7, step 3",
        "config": cfg.to_dict(), "seeds": seeds, "runs_per_seed": runs,
        "island": [s.to_dict() for s in default_island()],
        "scenarios": {k: {"label": lab, "supply_profile": prof or cfg.supply_profile, "lost_at_strike": list(lost)}
                      for k, lab, _, prof, lost in SCENARIOS},
        "sweep": {"supply_shares": list(SWEEP), "supply_profile": "70 % until the strike near hour 3, then the share",
                  "lost_at_strike": list(TWO), "two_sites_deep_is": DEEP_SHARE},
        "after_strike_s": AFTER_S,
        "pass_rule": "honest: the four architectures make the same decisions (commands and slots), every command "
                     "runs and every substation gets a fresh decision in every slot; one site lost: A and B fail over, "
                     "C and D decide exactly as without the loss, at most one slot missed; two sites lost at 45 %: B's "
                     "table trips no UFLS, C and D decide exactly as without the loss and trip no UFLS after the "
                     "strike; two sites lost at 25 %: B's table trips UFLS again in every run, C and D never after "
                     "the strike; no collapse anywhere. Sweep (two sites lost, supply after the strike 20 to 50 %): "
                     "C and D never trip UFLS after the strike or collapse; B's table trips UFLS again in every run "
                     "below its 40 % plan",
        "all_passed": passed,
        "known_issues": [
            "UFLS sheds whole stages until the load fits G(t), with no limit on the imbalance it can arrest, so "
            "B's table overloads end in UFLS, not in a collapse, unless the critical feeders alone exceed G(t) "
            "(UFLS exempts them: sweep point 20 %); with that limit (real UFLS schemes are designed for "
            "imbalances up to about 25 to 50 %) the same overloads could black the island out",
            "the policy pays for no UFLS and a fair rotation in energy: it plans with a 5 % reserve, whole "
            "feeders and the cold-load pickup of every feeder it restores, so after the strike at 50 % the "
            "customer demand it serves is about 70 % of G(t) (about 83 % with the pickup). Holding state (A "
            "without centres) and B's table plus UFLS pack the load up to G(t) and rotate less, so they can "
            "serve more energy (A from 35 %, B from 25 to 35 %); the price is UFLS events decided by relay "
            "stage instead of by the rules, and for A the same feeders dark all day (fairness 0.62 to 0.74)",
            "B's substations re-close feeders that UFLS tripped when the table says so; a scheme that locks tripped "
            "feeders out would trip less and serve less",
            "after losing both centres A holds its state (question 26): its feeders stay as they were, UFLS acts "
            "when the load grows and nothing rotates; after a collapse (sweep point 20 %) nothing re-closes, so "
            "the island is live again after the blackstart but every feeder stays open: dark_h counts only the "
            "hour before the blackstart, critical_served_share shows the rest",
            "the loads, the island and the supply profile are illustrative assumptions (loads.py, config.py), not "
            "measured data",
        ],
    }
    common.write_results(out, rows, meta)
    with open(out + ".svg", "w", encoding="utf-8") as fh:
        fh.write(svg(rows, seeds * runs))
    with open(out + "_sweep.svg", "w", encoding="utf-8") as fh:
        fh.write(svg_sweep(rows, seeds * runs))
    print("wrote", out + ".svg", "and", out + "_sweep.svg")
    print("all checks passed" if passed else "CHECK FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
