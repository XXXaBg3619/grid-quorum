"""Watches the live demo: polls every node's /status and shows the whole island.

    python -m gridq.live.monitor serve              # dashboard on :8080 (the compose service)
    python -m gridq.live.monitor table              # one console table, then exit
    python -m gridq.live.monitor json               # one summary as JSON (demo/scenario.py reads it)
    python -m gridq.live.monitor fault v3 equivocate    # or honest: switch a validator's behaviour

The summary is the monitor's own reading of the nodes' public state:
committing (a block within the last 2.5 slots), conflicting commits (two
validators with different blocks at one height), double-sign evidence (who
reported whom) and captured substations (a substation whose trusted block is
not the committee's).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence

from ..lasthop import default_feeders
from .runtime import STATUS_PORT, env_config, json_reply, start_http

DASHBOARD_PORT = 8080


def fetch(host: str, path: str = "/status", timeout: float = 0.6):
    try:
        with urllib.request.urlopen(f"http://{host.lower()}:{STATUS_PORT}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def poll(names: Sequence[str]) -> Dict[str, Optional[dict]]:
    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        return dict(zip(names, ex.map(fetch, names)))


def summarize(cfg, validators: Sequence[str], substations: Sequence[str], st: Dict[str, Optional[dict]]) -> dict:
    period = cfg.dispatch_period_s
    vs, hashes, evidence, events = [], {}, {}, []
    for v in validators:
        s = st.get(v)
        if not s:
            vs.append({"name": v, "up": False})
            continue
        last = s.get("last") or {}
        age = None if not last else round(s["t"] - last["t"], 1)
        vs.append({"name": v, "up": True, "height": s["height"], "round": s["round"], "step": s["step"],
                   "fault": s["fault"], "last_height": last.get("height", 0), "last_hash": last.get("hash"),
                   "last_age_s": age, "last_round": last.get("round"), "latency_s": s.get("latency_s", []),
                   "guard_refused": s.get("stats", {}).get("guard_refused", 0)})
        for h, x in s.get("hashes", {}).items():
            hashes.setdefault(int(h), {})[v] = x
        for e in s.get("evidence", []):
            evidence.setdefault(e["offender"], set()).add(v)
        events += s.get("events", [])
    up = [x for x in vs if x["up"]]
    height = max((x["last_height"] for x in up), default=0)
    ages = [x["last_age_s"] for x in up if x["last_age_s"] is not None and x["last_height"] == height]
    age = min(ages) if ages else None
    conflicts = sorted(h for h, d in hashes.items() if len(set(d.values())) > 1)
    agreed = {h: next(iter(d.values())) for h, d in hashes.items() if len(set(d.values())) == 1}
    subs, captured = [], []
    for name in substations:
        s = st.get(name)
        if not s:
            subs.append({"name": name, "up": False})
            continue
        tip = s["tip"]
        known = agreed.get(tip)
        bad = tip > height + 1 or (known is not None and known != s["tip_hash"])
        if bad:
            captured.append(name)
        subs.append({"name": name, "up": True, "tip": tip, "tip_hash": s["tip_hash"], "feeders": s["feeders"],
                     "executed": s["executed"], "rejected": s["rejected"], "version": s["version"],
                     "captured": bad, "behind": max(0, height - tip),
                     "applied_age_s": None if s.get("applied_at") is None else round(s["t"] - s["applied_at"], 1)})
        events += s.get("events", [])
    events.sort(key=lambda e: e["t"])
    return {"t": time.time(), "n": len(validators), "f": cfg.f, "q": cfg.quorum, "period_s": period,
            "up": len(up), "height": height, "last_block_age_s": age,
            "committing": age is not None and age < 2.5 * period,
            "conflicts": conflicts, "evidence": {k: sorted(v) for k, v in sorted(evidence.items())},
            "captured": captured, "validators": vs, "substations": subs, "events": events[-40:]}


def table(s: dict) -> str:
    out = [f"validators up {s['up']}/{s['n']} (Q = {s['q']}), newest block h={s['height']}"
           f" ({s['last_block_age_s']} s ago), committee {'COMMITTING' if s['committing'] else 'HALTED (holding)'}",
           f"conflicting commits: {len(s['conflicts'])} {s['conflicts'] or ''}   "
           f"double-sign evidence: {s['evidence'] or 'none'}   captured substations: {s['captured'] or 'none'}",
           "", f"{'node':5} {'height':>6} {'round':>5} {'step':10} {'fault':11} last block"]
    for v in s["validators"]:
        if not v["up"]:
            out.append(f"{v['name']:5} {'down':>6}")
            continue
        rnd, stp = (v["round"], str(v["step"])) if v["round"] >= 0 else ("-", "next slot")
        out.append(f"{v['name']:5} {v['height']:>6} {rnd:>5} {stp:10} {v['fault']:11} "
                   f"h={v['last_height']} {v['last_hash'] or ''} r={v['last_round']}")
    out += ["", f"{'sub':5} {'tip':>6} {'exec':>5}  feeders{'':16} rejected"]
    for x in s["substations"]:
        if not x["up"]:
            out.append(f"{x['name']:5} {'down':>6}")
            continue
        feeders = " ".join(f"{f.split('-')[-1]}:{st}" for f, st in x["feeders"].items())
        flag = "  CAPTURED" if x["captured"] else (f"  behind {x['behind']}" if x["behind"] > 1 else "")
        out.append(f"{x['name']:5} {x['tip']:>6} {x['executed']:>5}  {feeders:23} {x['rejected'] or '-'}{flag}")
    return "\n".join(out)


class Monitor:
    def __init__(self, cfg, validators: Sequence[str], substations: Sequence[str], every_s: float = 0.5):
        self.cfg, self.validators, self.substations, self.every = cfg, list(validators), list(substations), every_s
        self.latest: dict = {}

    def once(self) -> dict:
        return summarize(self.cfg, self.validators, self.substations, poll(self.validators + self.substations))

    def loop(self) -> None:
        while True:
            try:
                self.latest = self.once()
            except Exception as exc:               # keep the dashboard alive whatever a node returns
                self.latest = {"error": repr(exc), "t": time.time()}
            time.sleep(self.every)

    def http_get(self, path: str, query: dict):
        if path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", DASHBOARD.encode()
        if path == "/status.json":
            return json_reply(self.latest)
        return json_reply({"error": "not found"}, 404)


def set_fault(node: str, mode: str) -> dict:
    req = urllib.request.Request(f"http://{node.lower()}:{STATUS_PORT}/control", method="POST",
                                 data=json.dumps({"fault": mode}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.loads(r.read())


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Watch the live demo.")
    ap.add_argument("command", choices=("serve", "table", "json", "fault"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--port", type=int, default=DASHBOARD_PORT)
    a = ap.parse_args(argv)
    cfg = env_config()
    validators = [f"v{i}" for i in range(cfg.n_validators)]
    subs = sorted(default_feeders(int(os.environ.get("GRIDQ_SUBSTATIONS", "4"))))
    mon = Monitor(cfg, validators, subs)
    if a.command == "table":
        print(table(mon.once()))
    elif a.command == "json":
        print(json.dumps(mon.once()))
    elif a.command == "fault":
        if len(a.args) != 2 or a.args[1] not in ("honest", "equivocate"):
            raise SystemExit("usage: monitor fault <validator> honest|equivocate")
        print(set_fault(*a.args))
    else:
        start_http(a.port, mon.http_get)
        print(f"dashboard on http://localhost:{a.port}", flush=True)
        try:
            mon.loop()
        except KeyboardInterrupt:
            pass
    sys.stdout.flush()


DASHBOARD = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grid-quorum 即時展示</title>
<style>
:root {
  --bg: #f6f5f1; --card: #ffffff; --ink: #1d1d1b; --ink2: #55534e; --muted: #8a877f; --line: #e2dfd7;
  --good: #1f7a4d; --good-bg: #e3f2ea; --warn: #9a6200; --warn-bg: #fbefd9; --bad: #b3261e; --bad-bg: #fbe4e2;
  --on: #1f7a4d; --off: #8a877f; --accent: #2f5d9e;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #171716; --card: #222220; --ink: #ecebe6; --ink2: #b9b6ad; --muted: #8d8a82; --line: #34332f;
    --good: #5cc28f; --good-bg: #1c3328; --warn: #e3a845; --warn-bg: #3a2e17; --bad: #f08a80; --bad-bg: #3d1f1c;
    --on: #5cc28f; --off: #6f6c65; --accent: #8fb3e8;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 20px 16px 40px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--ink2); margin: 0 0 16px; }
h2 { font-size: 15px; margin: 22px 0 8px; color: var(--ink2); font-weight: 600; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { padding: 6px 10px; border-radius: 8px; background: var(--card); border: 1px solid var(--line); }
.chip b { font-weight: 600; }
.good { color: var(--good); background: var(--good-bg); border-color: transparent; }
.warn { color: var(--warn); background: var(--warn-bg); border-color: transparent; }
.bad { color: var(--bad); background: var(--bad-bg); border-color: transparent; }
.grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
.grid.subs { grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.card.down { opacity: .55; }
.card .name { font-weight: 600; display: flex; justify-content: space-between; gap: 6px; }
.tag { font-size: 12px; padding: 1px 6px; border-radius: 6px; font-weight: 500; }
.kv { color: var(--ink2); font-size: 13px; font-variant-numeric: tabular-nums; }
.feeders { display: flex; gap: 6px; margin: 8px 0 4px; flex-wrap: wrap; }
.feeder { font-size: 12px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); }
.feeder.on { color: var(--on); border-color: var(--on); }
.feeder.off { color: var(--off); border-style: dashed; }
.events { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px;
  font: 12.5px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; max-height: 360px; overflow: auto; }
.events div { white-space: pre-wrap; word-break: break-word; }
.events .reject, .events .evidence { color: var(--bad); }
.events .commit { color: var(--ink2); }
footer { color: var(--muted); font-size: 12px; margin-top: 18px; }
</style>
</head>
<body>
<main>
<h1>grid-quorum 即時展示：7 個驗證節點、4 個變電所</h1>
<p class="sub">標準 BFT，n = 7、f = 2、Q = 5。變電所只用公鑰驗 commit proof，簽章不足 Q 個、序號舊了、鏈不對都不執行。</p>
<div class="chips" id="chips"></div>
<h2>驗證節點</h2>
<div class="grid" id="validators"></div>
<h2>變電所（輕節點）</h2>
<div class="grid subs" id="subs"></div>
<h2>最近事件</h2>
<div class="events" id="events"></div>
<footer id="foot">每秒更新</footer>
</main>
<script>
const $ = (id) => document.getElementById(id);
const STEP = {propose: "等提案", prevote: "prevote 投票中", precommit: "precommit 投票中"};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
function chip(cls, text) { return `<span class="chip ${cls}">${text}</span>`; }
function render(s) {
  if (s.error || !s.validators) { $("foot").textContent = "等待節點回應… " + (s.error || ""); return; }
  const ev = Object.entries(s.evidence).map(([o, by]) => `${o}（${by.length} 個節點舉證）`).join("、");
  $("chips").innerHTML = [
    chip(s.up >= s.q ? "good" : "bad", `${s.up >= s.q ? "✓" : "✗"} 驗證節點 <b>${s.up}/${s.n}</b> 上線，提交需要 Q = ${s.q}`),
    chip(s.committing ? "good" : "warn", s.committing
      ? `✓ 出塊中：最新 h = <b>${s.height}</b>（${s.last_block_age_s} 秒前）`
      : `⏸ 停止出塊，變電所維持現狀（最新 h = ${s.height}）`),
    chip(s.conflicts.length ? "bad" : "good", `${s.conflicts.length ? "✗" : "✓"} 衝突提交 <b>${s.conflicts.length}</b>`),
    chip(ev ? "warn" : "", `雙簽證據：${ev || "無"}`),
    chip(s.captured.length ? "bad" : "good", s.captured.length
      ? `✗ 變電所被劫持：${s.captured.join("、")}` : "✓ 變電所都跟著委員會的鏈"),
  ].join("");
  $("validators").innerHTML = s.validators.map((v) => {
    if (!v.up) return `<div class="card down"><div class="name">${v.name}<span class="tag bad">離線</span></div><div class="kv">沒有回應</div></div>`;
    const tag = v.fault === "honest" ? `<span class="tag good">誠實</span>` : `<span class="tag bad">${esc(v.fault)}</span>`;
    return `<div class="card"><div class="name">${v.name}${tag}</div>
      <div class="kv">${v.round >= 0 ? `高度 ${v.height}・回合 ${v.round}` : `高度 ${v.height}`}</div>
      <div class="kv">${v.round >= 0 ? esc(STEP[v.step] || v.step || "") : "等下一個時槽"}</div>
      <div class="kv">已提交 h=${v.last_height}${v.last_age_s != null ? `（${Math.round(v.last_age_s)} 秒前）` : ""}</div></div>`;
  }).join("");
  $("subs").innerHTML = s.substations.map((x) => {
    if (!x.up) return `<div class="card down"><div class="name">${x.name}<span class="tag bad">離線</span></div></div>`;
    const tag = x.captured ? `<span class="tag bad">被劫持</span>`
      : x.behind > 1 ? `<span class="tag warn">落後 ${x.behind}</span>` : `<span class="tag good">同步</span>`;
    const feeders = Object.entries(x.feeders).map(([f, st]) =>
      `<span class="feeder ${st}">${esc(f)} ${st === "on" ? "供電" : "停電"}</span>`).join("");
    const rej = Object.entries(x.rejected).map(([r, n]) => `${esc(r)} ×${n}`).join("、") || "無";
    return `<div class="card"><div class="name">${x.name}${tag}</div><div class="feeders">${feeders}</div>
      <div class="kv">信任的區塊 h=${x.tip}・已執行 ${x.executed} 道指令</div><div class="kv">拒絕：${rej}</div></div>`;
  }).join("");
  $("events").innerHTML = s.events.slice().reverse().map((e) =>
    `<div class="${esc(e.kind)}">${esc(e.time)} ${esc(e.node).padStart(3)}  ${esc(e.text)}</div>`).join("");
  $("foot").textContent = `每秒更新・時槽 ${s.period_s} 秒・${new Date(s.t * 1000).toLocaleTimeString()}`;
}
async function tick() {
  try { render(await (await fetch("status.json", {cache: "no-store"})).json()); }
  catch (e) { $("foot").textContent = "監看程式沒有回應"; }
}
tick(); setInterval(tick, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
