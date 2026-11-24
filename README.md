# grid-quorum
戰時一個變成孤島的區域，即使調度中心被駭或被炸，剩下的電力仍照政府定的規則、依當下的電力狀態分配。

這是「區塊鏈導論」期末專題的模擬器：在同一套政策和情境下比較四種調度架構（A 中央調度加備援、B 中央調度加變電所在地規則檢查、C 台電內部 BFT、D 多組織 BFT），注入作惡節點、節點離線、網路分區、舊指令重放、組合式違規和共因失效，量測安全性、可用性與延遲。

> 目前進度：完成設計文件 §7 的第 1 到 3 步。v0.1 骨架從 evsim（BlokcChin@796b43c）搬來可直接沿用的模組並改成電網用途；加上逐節點的 BFT 共識（`gridq/consensus/`，已通過對理論值的檢查）、變電所輕節點驗證與最後一跳（`gridq/verifier.py`、`gridq/lasthop.py`）、exp4 舊指令重放（有序號或狀態版本檢查時，重放全部被擋下），以及簡化電力模型、合成負載、分配政策和 A／B／C／D 四種決策路徑（`gridq/power.py`、`loads.py`、`policy.py`、`central.py`、`sim.py`）。孤島檢查顯示：無故障時四種架構的決策完全相同；失去兩個站點、供電低於 B 預簽輪停表假設的 40% 時，B 的表一再觸發低頻卸載，C、D 依當下供電重算，打擊後沒有低頻卸載。下一步是其餘五種故障與 exp1、2、3、5、6。各模組的狀態見 [docs/DESIGN.md](docs/DESIGN.md)。

## 快速開始

只需 Python 3.10 以上，不用安裝套件。

```bash
python3 -m unittest discover -s tests     # 全部測試，約 20 秒
python3 experiments/check_consensus.py    # 共識對理論值的檢查，4 核約 1 分鐘
python3 experiments/exp4_replay.py        # exp4 舊指令重放，4 核約 1 分鐘
python3 experiments/check_island.py       # 四種架構在同一個孤島上（問題 22），4 核約 2 分鐘
bash experiments/run_all.sh               # 先跑測試，再跑已經寫好的實驗
```

## 目錄

```
gridq/              模擬器套件
  config.py         參數（f、Q 由驗證者數推出）
  kernel.py         事件佇列與時鐘
  events.py         雜湊鏈事件日誌
  network.py        延遲、遺失、斷線、分區
  crypto.py         模擬簽章
  chain.py          區塊頭雜湊鏈驗證
  theory.py         理論值，用來驗證模擬結果
  metrics.py        統計工具與指標（含孤島指標）
  svgchart.py       SVG 圖表
  faults.py         故障注入（已寫：重放、站點離線）
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
experiments/        實驗腳本，輸出 CSV 加 meta.json
  check_consensus.py  共識對理論值的檢查
  exp4_replay.py      exp4 舊指令重放
  check_island.py     四種架構在同一個孤島上的檢查與供電敏感度
results/main/       實驗結果（CSV、meta.json、SVG）
tests/              unittest
docs/DESIGN.md      設計原則、協議假設、模組狀態、實驗對照
```
