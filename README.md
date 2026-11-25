# grid-quorum
戰時一個變成孤島的區域，即使調度中心被駭或被炸，剩下的電力仍照政府定的規則、依當下的電力狀態分配。

這是「區塊鏈導論」期末專題的模擬器：在同一套政策和情境下比較四種調度架構（A 中央調度加備援、B 中央調度加變電所在地規則檢查、C 台電內部 BFT、D 多組織 BFT），注入作惡節點、節點離線、網路分區、舊指令重放、組合式違規和共因失效，量測安全性、可用性與延遲。

> 目前進度：完成設計文件 §7 的第 1 到 5 步，六個實驗都寫好、全部通過，加分項的 Docker 展示也完成（見下方「Docker 展示」）。v0.1 骨架從 evsim（BlokcChin@796b43c）搬來可直接沿用的模組並改成電網用途；加上逐節點的 BFT 共識（`gridq/consensus/`，已通過對理論值的檢查）、變電所輕節點驗證與最後一跳（`gridq/verifier.py`、`gridq/lasthop.py`）、簡化電力模型、合成負載、分配政策和 A／B／C／D 四種決策路徑（`gridq/power.py`、`loads.py`、`policy.py`、`central.py`、`sim.py`），以及六種故障（`gridq/faults.py`）和共因模型（`gridq/theory.py`）。
>
> 主要結果（數字見 [docs/DESIGN.md](docs/DESIGN.md) §7）：
> - 不超過 f 個雙重簽名、離線或被分開的驗證者時，從沒有兩個衝突的區塊被提交，訊息不遺失時每個時槽照常決策（遺失時要靠重送，見 §6）；超過 f 時正確停下、恢復後接上，f + 1 個雙簽者造成的衝突都可追責（exp1、exp2、exp3）。
> - 有序號或狀態版本檢查時，重放的舊指令全部被擋下（exp4）。
> - 被駭的調度中心一個節點就能讓 A、B 執行有害的指令；B 的在地檢查只擋得住明顯違規，擋不住「每道合規、合起來超載」的全部復電。C、D 要被攻破 5 個驗證者（Q）才偽造得了指令（exp5）。
> - C、D 被讀走全島資料只要攻破 1 個節點，癱瘓要 3 個，偽造要 5 個；A、B 三樣都只要 1 個。在示意的假設下，D 的組織比較多，被讀走的機會比 C 高，被癱瘓或偽造的機會比 C 低（exp6 的分界圖）。
> - Docker 展示用同一份共識和驗證程式跑在 11 個容器上（真的 Ed25519 簽章、UDP 網路）：停 2 個驗證者照常出塊，停 3 個或 4｜3 分區就停住、變電所維持現狀，恢復後接上；雙簽者被其他節點抓到；重放、竄改、2 把金鑰的偽造、別條鏈的區塊都被變電所拒絕；只有拿到 5 把金鑰才偽造得了（§7 第 5 步）。

## 快速開始

只需 Python 3.10 以上，不用安裝套件。

```bash
python3 -m unittest discover -s tests       # 全部 184 個測試，約 30 秒（沒有 cryptography 套件時跳過 8 個即時模式測試）
python3 experiments/check_consensus.py      # 共識對理論值的檢查，4 核約 1 分鐘
python3 experiments/check_island.py         # 四種架構在同一個孤島上（問題 22），4 核約 2 分鐘
python3 experiments/exp1_equivocation.py    # exp1 作惡節點，4 核約 3 分鐘
python3 experiments/exp2_offline.py         # exp2 節點離線，4 核約 3 分鐘
python3 experiments/exp3_partition.py       # exp3 分區、延遲、干擾，4 核約 2 分鐘
python3 experiments/exp4_replay.py          # exp4 舊指令重放，4 核約 1 分鐘
python3 experiments/exp5_joint_overload.py  # exp5 每道合規、合起來有害，4 核約 3 分鐘
python3 experiments/exp6_common_cause.py    # exp6 共因失效，約 10 秒
bash experiments/run_all.sh                 # 先跑測試，再跑全部實驗
```

每個實驗都可以加 `--quick` 做冒煙測試。執行時間有上限的機器可以分批：`--shard 0/4` 到 `--shard 3/4` 各跑四分之一，再用同樣的參數加 `--merge 4` 合併，結果和一次跑完相同。

## Docker 展示（加分項）

7 個驗證節點和 4 個變電所各跑在自己的容器裡，用真的 Ed25519 簽章和 UDP 網路，跑的是模擬器同一份共識、派工規則和變電所驗證程式（`gridq/live/`）。每個容器只掛載自己的私鑰和所有人的公鑰，變電所只用公鑰驗 commit proof。需要 Docker（Mac 用 Docker Desktop），本機的 Python 不用裝套件：

```bash
python3 demo/scenario.py               # 建映像、產生金鑰、啟動，依序跑 8 個情境並逐項檢查，約 8 分鐘
python3 demo/scenario.py --only 1,2,3  # 只跑其中幾個
python3 demo/scenario.py --down        # 跑完就關掉
```

跑的時候打開 <http://localhost:8080> 看即時儀表板（驗證節點的高度和回合、變電所的饋線、拒絕原因、最近事件）。8 個情境：

| | 情境 | 預期 |
|---|---|---|
| 1 | 正常運作 | 每 8 秒一個區塊，變電所驗過 5 個以上簽章才執行 |
| 2 | 停掉 v5、v6（= f） | 剩 5 個 = Q，照樣出塊 |
| 3 | 再停 v4（> f） | 剩 4 個 < Q，不出塊；變電所停在同一個區塊，饋線不動 |
| 4 | 重新啟動三個 | 補齊錯過的區塊、恢復出塊、沒有衝突 |
| 5 | 網路分區 4｜3（`docker network disconnect`） | 兩邊都湊不到 Q，都不出塊；接回後恢復 |
| 6 | v3 雙簽 | 其他節點互傳提案後抓到它，沒有衝突提交 |
| 7 | 最後一跳的攻擊：重放、竄改、2 把金鑰偽造、別條鏈 | 變電所分別以 stale_seq、insufficient_quorum、wrong_chain 拒絕 |
| 8 | 共因：同一組織的 5 把金鑰（C） | 偽造的區塊被執行，這就是 Q 的安全邊界；之後要重新開始 |

手動操作（在 repo 根目錄）：

```bash
docker compose -f demo/docker-compose.yml build
docker compose -f demo/docker-compose.yml run --rm keygen        # 第一次：產生 demo/keys/
GRIDQ_T0=$(date +%s) docker compose -f demo/docker-compose.yml up -d
docker compose -f demo/docker-compose.yml logs -f v0 S1          # 看節點的輸出
docker compose -f demo/docker-compose.yml exec monitor python -m gridq.live.monitor table
docker compose -f demo/docker-compose.yml stop v5 v6             # 停掉兩個驗證者
docker compose -f demo/docker-compose.yml exec monitor python -m gridq.live.monitor fault v3 equivocate
docker compose -f demo/docker-compose.yml run --rm attacker forge --keys v5,v6
docker compose -f demo/docker-compose.yml down
```

沒有 Docker 時，`python3 -m gridq.live.cluster --seconds 40 --period 4` 在單一行程裡跑同樣的 11 個節點（需要 `pip install cryptography`）。

## 目錄

```
gridq/              模擬器套件
  config.py         參數（f、Q 由驗證者數推出）
  kernel.py         事件佇列與時鐘
  events.py         雜湊鏈事件日誌
  network.py        延遲、遺失、斷線、分區
  crypto.py         模擬簽章
  chain.py          區塊頭雜湊鏈驗證
  theory.py         理論值，用來驗證模擬結果；共因模型
  metrics.py        統計工具與指標（孤島、故障期間、每個節點看得到什麼、被攻破的機率）
  svgchart.py       SVG 圖表
  faults.py         故障注入：離線、雙重簽名、重放、被駭的中心、被攻破的驗證者、觀察者
  arch/             A／B／C／D 四種架構
  consensus/        BFT 共識：逐節點狀態機、commit proof
  verifier.py       變電所驗證器（輕節點）
  substation.py     變電所那一端：收指令、補洞、B 的輪停表、狀態回報
  lasthop.py        C、D 的最後一跳：區塊送到變電所、狀態回報上鏈
  central.py        A、B 的調度中心與備援
  policy.py         分配政策（四種架構共用）
  power.py          簡化電力模型：G(t)、冷載、低頻卸載、崩潰
  loads.py          合成負載與預設孤島
  sim.py, run.py    模擬器主體與配對執行
  live/             即時模式（Docker 展示）：Ed25519 金鑰、真實時鐘與 UDP、驗證節點、變電所、攻擊者、監看儀表板
experiments/        實驗腳本，輸出 CSV 加 meta.json
  check_consensus.py  共識對理論值的檢查
  check_island.py     四種架構在同一個孤島上的檢查與供電敏感度
  exp1_equivocation.py ... exp6_common_cause.py  六個故障實驗
  run_all.sh          一次重跑全部
results/main/       實驗結果（CSV、meta.json、SVG）
demo/               Docker 展示：Dockerfile、docker-compose.yml、情境腳本 scenario.py
tests/              unittest
docs/DESIGN.md      設計原則、協議假設、模組狀態、實驗對照
```
