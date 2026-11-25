#!/usr/bin/env python3
"""Runs the Docker demo's scenarios one after another and checks each one (Python 3.8+, standard library).

    python3 demo/scenario.py               # build, start, run scenarios 1-8, leave the demo running
    python3 demo/scenario.py --only 1,2,3  # some of them (always on a fresh start)
    python3 demo/scenario.py --down        # stop and remove the demo at the end
    python3 demo/scenario.py --keep-going  # do not stop at the first failed check

Open http://localhost:8080 while it runs to watch the dashboard. Scenario 8
hands the substations to the attacker, so it runs last; start again to reset.

  1 honest operation           blocks every slot, substations execute them
  2 two validators stopped     f = 2 down: 5 = Q remain, blocks continue
  3 a third one stopped        4 < Q: no block, substations hold their state
  4 all restarted              they catch up, blocks resume, no conflicts
  5 network partition 4 | 3    real network cut: no side has Q, both hold; heal
  6 an equivocating validator  v3 sends two blocks in its rounds: caught, no split
  7 last-hop attacks           replay, tamper, forge with 2 keys, wrong chain: refused
  8 common cause (C)           an attacker with 5 keys of one organisation: executed
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = ["docker", "compose", "-f", os.path.join(HERE, "docker-compose.yml")]
NETWORK = "gridq_grid"
PERIOD = float(os.environ.get("GRIDQ_PERIOD", "8"))
RESULTS = []


# ---------------------------------------------------------------- docker

def compose(*args, capture=False, check=True):
    r = subprocess.run(COMPOSE + list(args), capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.stderr.write((r.stderr or "") + "\n")
        raise SystemExit(f"failed: docker compose {' '.join(args)}")
    return r.stdout if capture else r.returncode


def container(service):
    return compose("ps", "-q", service, capture=True).strip()


def monitor(*args):
    return compose("exec", "-T", "monitor", "python", "-m", "gridq.live.monitor", *args, capture=True)


def status():
    for _ in range(5):
        try:
            return json.loads(monitor("json"))
        except (SystemExit, ValueError):
            time.sleep(1.0)
    raise SystemExit("the monitor does not answer")


def show():
    print("\n" + monitor("table"))


def attack(*args):
    out = compose("run", "--rm", "-T", "attacker", *args, capture=True)
    print("\n".join("    " + line for line in out.strip().splitlines()))
    return out


# ---------------------------------------------------------------- checks

def step(n, title, what):
    print(f"\n{'=' * 78}\n[{n}] {title}\n    {what}\n{'=' * 78}", flush=True)


def check(label, ok, detail=""):
    RESULTS.append((label, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok and not ARGS.keep_going:
        show()
        raise SystemExit("a check failed; the demo is still running (docker compose ... down to stop it)")


def wait_for(cond, timeout, label):
    end = time.time() + timeout
    s = status()
    while not cond(s) and time.time() < end:
        time.sleep(1.0)
        s = status()
    print(f"  ... {label}: {'yes' if cond(s) else 'no'} after {timeout - max(0, end - time.time()):.0f} s", flush=True)
    return s


def pause(slots, why):
    print(f"  ... waiting {slots * PERIOD:.0f} s ({slots} slots) {why}", flush=True)
    time.sleep(slots * PERIOD)


def up_validators(s):
    return [v for v in s["validators"] if v["up"]]


def in_step(s):
    """Every running validator has committed the newest height (none stuck behind)."""
    return all(v["last_height"] >= s["height"] - 1 for v in up_validators(s))


def subs_follow(s):
    return all(x["up"] and not x["captured"] and x["behind"] <= 1 for x in s["substations"])


# ---------------------------------------------------------------- scenarios

def s1_honest():
    step(1, "正常運作", "7 個驗證節點每個時槽出一個區塊，變電所驗過 5 個以上簽章才執行")
    h0 = status()["height"]
    s = wait_for(lambda s: s["height"] >= h0 + 3 and subs_follow(s), 6 * PERIOD, "three more blocks")
    check("委員會持續出塊", s["height"] >= h0 + 3, f"h {h0} -> {s['height']}")
    check("沒有衝突提交", not s["conflicts"])
    check("變電所都跟上最新區塊並執行指令", subs_follow(s) and all(x["executed"] > 0 for x in s["substations"]))
    show()


def s2_two_down():
    step(2, "停掉 2 個驗證節點（v5、v6）", "f = 2：剩 5 個 = Q，照樣能出塊（輪到離線節點提案的回合會等逾時）")
    compose("stop", "v5", "v6")
    h0 = status()["height"]
    s = wait_for(lambda s: s["height"] >= h0 + 3, 7 * PERIOD, "three more blocks with 5 validators")
    check("只剩 5 個仍持續出塊", s["height"] >= h0 + 3 and s["up"] == 5, f"h {h0} -> {s['height']}, up {s['up']}")
    check("沒有衝突提交", not s["conflicts"])
    check("變電所照常跟上", subs_follow(s))
    show()


def s3_three_down():
    step(3, "再停掉 v4", "剩 4 個 < Q：不可能湊到 5 個簽章，委員會停住，變電所維持現狀（fail-safe hold）")
    compose("stop", "v4")
    pause(1.5, "for the block in flight")
    s0 = status()
    pause(4, "with 4 validators")
    s = status()
    check("4 個驗證節點不出任何區塊", s["height"] == s0["height"] and not s["committing"], f"h stays {s['height']}")
    check("變電所停在同一個區塊、饋線狀態不變",
          [x["tip"] for x in s["substations"]] == [x["tip"] for x in s0["substations"]]
          and [x["feeders"] for x in s["substations"]] == [x["feeders"] for x in s0["substations"]])
    check("沒有衝突提交", not s["conflicts"])
    show()


def s4_restart():
    step(4, "重新啟動 v4、v5、v6", "重啟的節點向其他節點補齊錯過的區塊；簽章守門員記得重啟前簽到哪裡，不會重簽")
    h0 = status()["height"]
    compose("start", "v4", "v5", "v6")
    s = wait_for(lambda s: s["up"] == 7 and s["height"] >= h0 + 2 and in_step(s) and s["committing"],
                 10 * PERIOD, "all 7 back and in step")
    check("7 個都回來而且跟上最新高度", s["up"] == 7 and in_step(s), f"h {h0} -> {s['height']}")
    check("恢復出塊、沒有衝突提交", s["committing"] and not s["conflicts"])
    check("變電所跟上", subs_follow(s))
    show()


def s5_partition():
    step(5, "網路分區：v4、v5、v6 和其他節點斷線（4 | 3）",
         "用 docker network disconnect 真的切斷；兩邊都湊不到 Q，都不出塊，不會分裂成兩條鏈")
    cut = ["v4", "v5", "v6"]
    for v in cut:
        subprocess.run(["docker", "network", "disconnect", NETWORK, container(v)], check=True)
    pause(1.5, "for the block in flight")
    s0 = status()
    pause(4, "while partitioned")
    s = status()
    check("分區期間不出塊", s["height"] == s0["height"] and not s["committing"], f"h stays {s['height']}")
    check("沒有衝突提交", not s["conflicts"])
    for v in cut:                                    # the service name is a network alias: give it back
        subprocess.run(["docker", "network", "connect", "--alias", v, NETWORK, container(v)], check=True)
    print("  ... network healed")
    s = wait_for(lambda s: s["up"] == 7 and s["height"] >= s0["height"] + 2 and in_step(s) and s["committing"],
                 10 * PERIOD, "blocks resume after healing")
    check("接回後恢復出塊、全部同一高度", s["committing"] and in_step(s), f"h -> {s['height']}")
    check("沒有衝突提交", not s["conflicts"])
    show()


def s6_equivocation():
    step(6, "v3 變成雙簽節點", "輪到 v3 提案時，它把兩個不同的區塊分給兩半的節點；其他節點互傳提案後抓到它")
    print("    " + monitor("fault", "v3", "equivocate").strip())
    h0 = status()["height"]
    s = wait_for(lambda s: len(s["evidence"].get("v3", [])) >= 4, 12 * PERIOD, "evidence against v3")
    check("至少 4 個誠實節點舉證 v3 雙簽", len(s["evidence"].get("v3", [])) >= 4, f"reported by {s['evidence'].get('v3')}")
    check("沒有衝突提交（1 個雙簽者 < f + 1）", not s["conflicts"])
    check("委員會照常出塊", s["committing"] and s["height"] > h0, f"h {h0} -> {s['height']}")
    print("    " + monitor("fault", "v3", "honest").strip())
    show()


def s7_last_hop():
    step(7, "最後一跳的攻擊", "攻擊者直接把區塊送到變電所：重放、竄改、只偷到 2 把金鑰的偽造、別條鏈的區塊")
    expected = [("replay", ["replay"], "stale_seq"),
                ("tamper", ["tamper"], "insufficient_quorum"),
                ("forge with 2 stolen keys", ["forge", "--keys", "v5,v6"], "insufficient_quorum"),
                ("wrong chain", ["wrong-chain", "--keys", "v5,v6"], "wrong_chain")]
    for label, args, reason in expected:
        before = {x["name"]: x["rejected"].get(reason, 0) for x in status()["substations"]}
        print(f"  {label}:")
        attack(*args)
        s = status()
        grew = all(x["rejected"].get(reason, 0) > before[x["name"]] for x in s["substations"])
        check(f"4 個變電所都以 {reason} 拒絕", grew)
    s = wait_for(lambda s: subs_follow(s) and s["committing"], 3 * PERIOD, "substations still follow the committee")
    check("變電所仍只跟委員會的鏈", subs_follow(s) and not s["captured"])
    show()


def s8_common_cause():
    step(8, "共因失效：同一個組織的 5 個站被一起攻破（C 架構）",
         "5 把金鑰 = Q，偽造的 commit proof 是有效的；D 要同時攻破 5 個不同組織才會這樣")
    attack("forge", "--keys", "v2,v3,v4,v5,v6")
    s = wait_for(lambda s: len(s["captured"]) == len(s["substations"]), 3 * PERIOD, "substations captured")
    check("4 個變電所都執行了攻擊者的區塊（全部停電）",
          len(s["captured"]) == len(s["substations"])
          and all(set(x["feeders"].values()) == {"off"} for x in s["substations"]))
    print("  (這是預期中的結果：Q 把金鑰就是 BFT 的安全邊界。重新開始：python3 demo/scenario.py)")
    show()


SCENARIOS = {1: s1_honest, 2: s2_two_down, 3: s3_three_down, 4: s4_restart, 5: s5_partition,
             6: s6_equivocation, 7: s7_last_hop, 8: s8_common_cause}


def posix_tz():
    off = time.localtime().tm_gmtoff // 60
    sign = "-" if off >= 0 else "+"                  # POSIX TZ counts west of UTC as positive
    h, m = divmod(abs(off), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def start():
    keys = os.path.join(HERE, "keys", "public.json")
    os.environ["GRIDQ_T0"] = str(int(time.time()))
    os.environ.setdefault("GRIDQ_TZ", posix_tz())
    os.environ["GRIDQ_PERIOD"] = f"{PERIOD:g}"
    print("building the image ...", flush=True)
    compose("build", "--quiet", capture=True)
    if not os.path.exists(keys):
        compose("run", "--rm", "-T", "keygen", capture=True)
        print("made fresh Ed25519 keys in demo/keys/")
    compose("down", "--remove-orphans", capture=True)
    compose("up", "-d", capture=True)
    print("started 7 validators, 4 substations and the monitor: http://localhost:"
          + os.environ.get("GRIDQ_DASHBOARD_PORT", "8080"), flush=True)
    s = wait_for(lambda s: s["height"] >= 2 and subs_follow(s), 12 * PERIOD, "first blocks")
    check("demo is up", s["height"] >= 2)


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated scenario numbers, e.g. 1,2,3")
    ap.add_argument("--down", action="store_true", help="stop and remove the demo at the end")
    ap.add_argument("--keep-going", action="store_true")
    ARGS = ap.parse_args()
    chosen = sorted({int(x) for x in ARGS.only.split(",") if x}) or sorted(SCENARIOS)
    start()
    try:
        for n in chosen:
            SCENARIOS[n]()
    finally:
        failed = [label for label, ok in RESULTS if not ok]
        print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed" + (f"; failed: {failed}" if failed else ""))
        if ARGS.down:
            compose("down", "--remove-orphans", capture=True)
            print("demo stopped and removed")
        else:
            print("the demo keeps running: http://localhost:8080 ; stop it with "
                  "docker compose -f demo/docker-compose.yml down")
    sys.exit(1 if [1 for _, ok in RESULTS if not ok] else 0)


if __name__ == "__main__":
    main()
