# grid-quorum 設計說明（v0.1 骨架）

> 依據：專題文件「戰時電網緊急分配：尖銳問題、回答與自承限制」（2026-11-24 定稿，「下一步：實作範圍與順序」一節）。
> 這一版只有骨架：從 evsim（BlokcChin@796b43c）搬來可直接沿用的模組，加上待實作模組的介面說明。

## 1. 目標

在同一套政策和輸入情境下比較 A／B／C／D 四種調度架構，注入六種故障，量測安全性、可用性與延遲。結論稱為「在指定協議假設下的模型驗證」，不是對實際 BFT 引擎的形式證明。

| 架構 | 做法 | 擋得住 | 擋不住或代價 |
|---|---|---|---|
| A 中央調度加備援 | 一個調度中心，備援中心隨時接手 | 調度中心被炸或斷線 | 被駭時，備援照樣執行錯誤的指令 |
| B 中央調度加在地規則檢查 | 變電所的檢查器拒絕違規指令；斷線時跑事先簽好的輪停表；加上低頻卸載 | 明顯違規的單一指令 | 單看合規、合起來有害的指令；中央失能後只能跑固定備案 |
| C 台電內部 BFT | 台電在自己的 7 個站點各跑一個節點 | 單一站點被駭或被炸；能做全域檢查與動態分配 | 同一管理網域，一次入侵可能打下多個節點；資料全來自台電 |
| D 多組織 BFT | 7 個組織各跑一個節點，f = 2，Q = 5 | 被駭加失聯合計不超過 f 個，一個組織出事只影響一個節點 | 成本和複雜度最高；最弱的組織拉低整體安全；共同輸入資料仍可能是假的 |

## 2. 設計原則（沿用 evsim，改成電網用途）

1. **同一套引擎、同一套政策、同一組情境**：A／B／C／D 只差在決策怎麼送到變電所，結果的差異才能歸因於架構。
2. **政策是純函式、與輸入順序無關**：每個驗證者算出同樣的決策，任何人都能從日誌重算。
3. **真相與可觀察分開**：事件分 `public` 與 `truth`。架構和驗證器只能讀 `public`；故障動作和遺失的訊息只記在 `truth`，給指標用。
4. **指標只從日誌算**：`metrics.py` 不碰架構的內部狀態。
5. **比較一律用配對差（共同亂數）**：網路、負載、故障的亂數都依 key 抽，誠實組和故障組、以及不同架構之間，看到同樣的網路和負載。
6. **只用標準函式庫**：Python 3.10 以上，測試用 unittest。
7. **結果可追溯**：每個實驗輸出 CSV 加 meta.json（設定、git commit、已知問題）。

## 3. 協議與故障模型

- 標準 BFT：f = ⌊(n − 1) / 3⌋，Q = ⌊2n / 3⌋ + 1（超過三分之二，和 CometBFT 一致）。n = 7 時 f = 2、Q = 5；n = 4 時 f = 1、Q = 3。`config.py` 由 n 推出 f 和 Q，測試檢查任兩個 Q 至少重疊 f + 1 個節點，而且 f 個節點沉默時仍能提交。
- f 把被駭和失聯的節點合計。活性假設部分同步：每一輪的逾時遞增（`GridConfig.round_timeout(r)`），網路恢復後總有一輪夠長。
- 驗證者集合固定（epoch 不變），動態成員與熱備援列為未來工作。
- 變電所是輕節點：檢查鏈 ID、epoch、高度與序號、饋線與絕對目標狀態、狀態版本、commit proof（至少 Q 個不同驗證者的有效簽章）和區塊頭鏈；TTL 只是次要檢查。拒絕理由的代碼在 `verifier.REJECT_REASONS`。
- 保守降級：湊不滿 Q 或沒有新的提交時，變電所維持現狀，只准在地保護性卸載（過載、UFLS）；不自動復閉，也不切換到備案輪停表。只有 B 有靜態備案，那是 B 的定義。
- 簽章：標準函式庫沒有 Ed25519，用每個節點一把金鑰的 HMAC 代替（`crypto.py`）。攻破 k 個節點的攻擊者持有 k 把金鑰，只做得出 k 個有效簽章。

## 4. 模組與狀態

| 模組 | 職責 | 來源 | 狀態 |
|---|---|---|---|
| `gridq/hashing.py` | 標準化 JSON 的 sha256 | evsim `model.canonical_hash` | 完成 |
| `gridq/events.py` | 只能附加的雜湊鏈事件日誌、public／truth、JSONL | evsim `events.py`，事件種類換成電網用 | 完成 |
| `gridq/kernel.py` | 時鐘與事件佇列：同一時間先依優先序、再依排入順序 | evsim `engine.py` 的排程核心 | 完成 |
| `gridq/config.py` | `GridConfig`：架構、委員會、網路、調度、電力參數；f、Q 由 n 推出 | evsim `config.py` 的寫法 | 完成，電力參數暫定 |
| `gridq/network.py` | 節點對節點的延遲、iid 遺失、Gilbert-Elliott 斷線、分區；亂數依 key 抽 | evsim `network.py`，從住戶連線推廣 | 完成 |
| `gridq/crypto.py` | 模擬簽章（HMAC） | 新寫 | 完成 |
| `gridq/chain.py` | 區塊頭雜湊鏈與驗證（完整性、副本、檢查點） | evsim `m1.log_entry`／`verify_log`，推廣到任意區塊格式 | 完成 |
| `gridq/theory.py` | `round_success_prob`：訊息遺失下一輪共識成功的機率 | evsim `m2.py`，改成傳入 Q | 部分：延遲上界、共因模型待寫 |
| `gridq/metrics.py` | `mean_ci95`、`jain`、`paired_diff` | evsim `metrics.py` | 部分：電網指標待寫 |
| `gridq/svgchart.py` | 不靠外部套件的折線圖（CI 帶、參考線、深色模式） | evsim `svgchart.py`，原樣 | 完成 |
| `gridq/faults.py` | 故障掛勾，預設誠實；同一個故障物件套到四種架構 | evsim `attacks.py` 的寫法 | 介面；六種故障待寫 |
| `gridq/arch/` | A／B／C／D 與共同介面 `Architecture` | evsim `mechanisms/base.py` 的寫法 | 介面；四種架構待寫 |
| `gridq/consensus/` | 逐節點的 BFT 共識（提案、prevote、precommit、鎖、換輪） | 新寫，取代 evsim 的 M2 | 待寫 |
| `gridq/verifier.py` | 變電所輕節點驗證 | 新寫 | 拒絕理由已定；檢查待寫 |
| `gridq/policy.py` | 政府規則（純函式）、在地檢查與全域檢查 | evsim `allocation.py` 的原則 | 待寫 |
| `gridq/power.py` | G(t)、饋線、冷載、UFLS、狀態版本 | 新寫 | 待寫 |
| `gridq/loads.py` | 醫院、加壓站、基地台、避難所、住宅負載 | 新寫 | 待寫 |
| `gridq/sim.py` | 一個孤島、一種架構、一次執行 | evsim `engine.py` 的分工 | 待寫 |
| `gridq/run.py` | 誠實組對故障組的配對執行 | evsim `run.py` | 待寫 |
| `experiments/common.py` | 實驗腳本共用：參數、平行執行、CSV 加 meta.json | evsim 實驗腳本的骨架 | 完成 |
| `experiments/run_all.sh` | 一次重跑全部實驗，先跑測試 | evsim `run_c3.sh` | 完成，實驗腳本寫好後自動加入 |

## 5. 六個實驗

| 實驗 | 注入 | 要驗證 | 主要模組 |
|---|---|---|---|
| exp1 作惡節點 | 一個節點對不同節點提出不同指令 | 容錯範圍內不會有兩份衝突決策都被提交 | consensus、`faults.proposal`／`vote` |
| exp2 節點離線 | 一到三個驗證者停止回應 | 故障不超過 f 時能繼續，超過時正確停止提交 | consensus、`faults.node_up` |
| exp3 分區或延遲 | 部分節點互相不通、訊息延遲 | 安全是否維持、活性何時下降、何時換輪或降級 | network 的分區、consensus |
| exp4 舊指令重放 | 重送已執行過的合法指令 | 變電所拒絕舊序號或不符的狀態版本 | verifier、chain |
| exp5 合規但組合後超載 | 多筆單獨合法的復電指令同時提出 | B 的在地檢查和 C、D 的全域檢查有何差別 | policy、power、arch B／C／D |
| exp6 共因失效 | 同一組織的多個節點一起被攻下 | C 和 D 在什麼獨立性假設下比較耐打（邊界圖） | theory、`faults.compromised`、svgchart |

指標：衝突提交、違規或過期指令的接受率、提交延遲、可用率、關鍵負載供電時數、孤島崩潰次數、C 對 D 的破壞難易度（超過 f 個節點被攻下的機率），以及攻破一個節點能看到多少敏感資料。

## 6. 刻意的簡化（之後可改）

- 分區期間跨群組的訊息直接丟掉，不排隊，靠協議在後面的輪次重送。
- 遺失和分區在送出時判定；分區開始前送出的訊息照常送達。
- 斷線模型以節點為單位：任一端斷線，訊息就遺失。
- 簽章是對稱式 HMAC，驗證端持有所有金鑰，只模擬「沒有金鑰就簽不出來」。
- 電力物理只抽象成可供電上限 G(t)、饋線負載與權重、冷載倍率、超載時的簡化 UFLS；不做潮流與保護暫態。

## 7. 下一步

依專題文件的順序：

1. `consensus/`：逐節點共識，先通過「無故障、iid 遺失下的成功率符合 `theory.round_success_prob`」和「衝突提交為零」兩項檢查。
2. `verifier.py` 接上 commit proof 與 `chain.py`，做 exp4。
3. `power.py`、`loads.py`、`policy.py`，再接上 `sim.py`、`run.py` 和 A／B／C／D。
4. `faults.py` 的六種故障與 `experiments/exp1` 到 `exp6`；`theory.py` 補上共因模型，畫 C 對 D 的邊界圖。
5. 加分項：用 Docker 跑 7 個節點加一個變電所驗簽腳本。

## 8. 來源

可直接沿用的程式從 evsim 複製（BlokcChin repo 的 `evsim-v1/`，分支 `evsim-v2-todo`，commit 796b43c），沒有帶舊的 git 歷史。evsim 的充電實驗結果留在原 repo，報告中當集合住宅的反例引用。
