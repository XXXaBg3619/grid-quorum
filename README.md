# grid-quorum
戰時一個變成孤島的區域，即使調度中心被駭或被炸，剩下的電力仍照政府定的規則、依當下的電力狀態分配。

這是「區塊鏈導論」期末專題的模擬器：在同一套政策和情境下比較四種調度架構（A 中央調度加備援、B 中央調度加變電所在地規則檢查、C 台電內部 BFT、D 多組織 BFT），注入作惡節點、節點離線、網路分區、舊指令重放、組合式違規和共因失效，量測安全性、可用性與延遲。

> 目前進度：v0.1 骨架（從 evsim，BlokcChin@796b43c，搬來可直接沿用的模組並改成電網用途），加上逐節點的 BFT 共識（`gridq/consensus/`，已通過對理論值的檢查）、變電所輕節點驗證與最後一跳（`gridq/verifier.py`、`gridq/lasthop.py`），以及 exp4 舊指令重放（有序號或狀態版本檢查時，重放全部被擋下）。其餘模組只有介面說明。各模組的狀態與下一步見 [docs/DESIGN.md](docs/DESIGN.md)。

## 快速開始

只需 Python 3.10 以上，不用安裝套件。

```bash
python3 -m unittest discover -s tests     # 全部測試，約 10 秒
python3 experiments/check_consensus.py    # 共識對理論值的檢查，4 核約 1 分鐘
python3 experiments/exp4_replay.py        # exp4 舊指令重放，4 核約 1 分鐘
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
  metrics.py        統計工具與指標
  svgchart.py       SVG 圖表
  faults.py         故障注入（已寫：重放）
  arch/             A／B／C／D 四種架構（介面）
  consensus/        BFT 共識：逐節點狀態機、commit proof
  verifier.py       變電所驗證器（輕節點）
  lasthop.py        最後一跳：區塊送到變電所、補洞同步、狀態回報上鏈
  policy.py         分配政策（待寫）
  power.py          簡化電力模型（待寫）
  loads.py          合成負載（待寫）
  sim.py, run.py    模擬器主體與配對執行（待寫）
experiments/        實驗腳本，輸出 CSV 加 meta.json
  check_consensus.py  共識對理論值的檢查
  exp4_replay.py      exp4 舊指令重放
results/main/       實驗結果（CSV、meta.json、SVG）
tests/              unittest
docs/DESIGN.md      設計原則、協議假設、模組狀態、實驗對照
```
