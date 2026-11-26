# 量測：指標、效能與限制

本檔說明合成測資的指標、效能、`eval.py` 輸出與已知限制。先讀 §1 的警語：342 題是合成測資，不是準確率。

## 1. 合成測資：回歸訊號，不是準確率

> **這些數字是合成的。** 342 題由對照表模擬 STT 同音錯誤（先過 OpenCC 簡化，模擬真實引擎的
> 輸出字形），不是真實錄音。用途只有一個：**規則壞掉時會掉下來**。
> 它們**不是**對外宣稱的準確率，也不該當設計依據。

| 指標 | 數值 |
|---|---|
| 簡體全名／漏字／英文／贅詞 top-1 | 1.000 |
| 繁體全名 top-1（對照組，`sc_vs_tc_gap`） | 1.000（gap 0.000） |
| 疑難查詢（同音、只講姓、只講名、部門+姓）recall@3 | 1.000 |
| 部門辨識（只講部門） | 1.000 |
| 疑難查詢觸發澄清的比例 | 0.990 |
| 無歧義查詢被多問一次的比例 | 0.230（其中多數真的有名冊內的同音鄰居） |
| **誤轉率**（沒澄清且答錯） | **0.000** |
| 同音＋報了部門 top-1（`homophone_dept_top1`） | **1.000** |
| 亂句硬抓到人的比例（16 句） | 0.000 |
| 資訊不足時不追問的比例（只講姓／只有稱謂／漏字） | 1.000 |
| 第 2 輪仍歧義時**又**問第三次的比例 | 0.000 |

**消融**（同音＋報部門的 top-1）：部門加權與部門收斂單獨開啟都能到 1.000，
兩者都關掉是 0.444；現況兩者都開。分工與門檻理由見 [`decision-logic.md#scoring`](decision-logic.md#scoring)。

## 2. 效能（純 Python，單核）

| 名冊人數 | 首音節桶＋低信心回掃 | 暴力全掃 | 加速 |
|---|---|---|---|
| 40 | 1.96 ms | 5.24 ms | 2.7× |
| 200 | 2.00 ms | 23.95 ms | 12.0× |
| 500 | 4.84 ms | 63.99 ms | 13.2× |
| 2000 | 12.09 ms | 206.99 ms | 17.1× |

兩個關鍵優化：查詢的拼音特徵只算一次；首音節桶＋低信心回退全掃（同音字的拼音首音節
必然相同，所以桶不會漏掉同音候選）。

<a id="call-tests"></a>
## 3. 真實通話：`eval.py --calls`

`eval.py --calls` 可以吃「通話日誌＋`truth.csv`」算同一套指標：通話中 AGI 只寫 JSONL
（逐字稿＋決策），撥完之後才把正確答案接上去評分，**答案卡不參與通話當下的任何決策**。
20 通的撥號、判準與 `truth.csv` 產生方式見 [`calls.md`](calls.md) §4 與 §6；
電話層的失敗分派見 [`agi-protocol.md`](agi-protocol.md#errors)。

**本 repo 不含任何真實通話語料**（含個資）。真實錯誤率（WER／CER）請用自己的錄音；
本 repo 的 stt_batch.py 以字元級 CER 計算。

## 4. `eval.py` 的 13 種 `kind`

**除 `english` 外，全部以簡體送出**（模擬真實 STT 的輸出字形，經 OpenCC `t2s` 轉換）。
`-sc` 後綴就是這個意思。

| `kind` | 測什麼 |
|---|---|
| `exact-sc` | 簡體全名（**主要情境**） |
| `exact-tc` | 繁體全名（對照組：少數引擎輸出繁體／人工輸入） |
| `homophone-sc` | 同音字替換 + 簡體（最真實的錯誤組合） |
| `homophone-dept-sc` | 同音字替換 + **報了部門**（部門是封閉詞彙，應該足以解掉同音歧義 → top-1 必須對） |
| `drop-char-sc` | 漏中間一個字 + 簡體 |
| `surname-only-sc` | 只講姓 + 稱謂 |
| `given-name-only-sc` | 只講名字 |
| `dept-only-sc` | 只講部門 |
| `dept+surname-sc` | 部門 + 姓 + 稱謂 |
| `english` | 英文別名（ASCII，不受簡繁影響） |
| `filler-sc` | 前後加贅詞（「麻烦」是簡體，用來測 S2T） |
| `garbage` | **完全不是要找人的句子**（訂便當、問地址、談業務合作…）→ 正確行為是「零候選」 |
| `nickname-trap` | 日常稱謂（王伯伯／陳董）→ 命中同姓算可接受，但**必須進澄清** |

## 5. `eval.py` 的輸出

`eval.py` 產出「一份檔案 + 三段 stdout」。四者刻意分開：檔案給程式比對，stdout 給人看。

```
out/eval_rows.json    342 筆逐列結果（12 欄）
out/eval_summary.json n + 17 個彙總指標
stdout ①              cases / 17 指標（= summary 的內容）
stdout ②              門檻掃描表（5 個 floor）
stdout ③              失敗案例清單（top3 全落空的）
```

`out/` 已在 `.gitignore`（每次跑都變）。要留證據就貼進 issue／PR，不要進版控。

### 5.1 `out/eval_rows.json`：12 欄

| # | 欄位 | 型別 | 怎麼算 | 沒有值的時候 |
|---|---|---|---|---|
| 1 | `query` | str | 實際送進 `Directory.search()` 的字串（簡體） | — |
| 2 | `kind` | str | 測資類型（13 種，見 §4） | — |
| 3 | `top1` | str \| null | 第一名候選的 `pid` | 無候選 → `null` |
| 4 | `top1_name` | str \| null | 該人的顯示名（繁） | 無候選 → `null` |
| 5 | `top1_score` | float | 該人的分數 | 無候選 → `0.0` |
| 6 | `margin` | float | `top1 − top2`（前二名差距） | 候選 < 2 → `1.0`（不是 0） |
| 7 | `hit1` | bool | `top1 ∈ expect` | `expect` 為空（dept-only）→ `false` |
| 8 | `hit3` | bool | 前三名任一 `∈ expect` | 同上 → `false` |
| 9 | `dept_ok` | bool \| null | 偵測到的部門 == `dept_expect` | 只有 `dept-only-sc` 有值，其餘 `null` |
| 10 | `flagged` | bool \| null | `needs_clarification()` 的結果（要不要追問） | `dept-only-sc` → `null`（沒有澄清問題） |
| 11 | `why` | str | 為什麼要／不要澄清（[`glossary.md`](glossary.md) §8）或 `dept-only` | — |
| 12 | `hit1_or_any` | bool | 「有沒有抓到任何一個人」（`top1 != null`） | 只用來判 `garbage`：亂句不該有候選 |

**`expect` 是「可接受的 pid 集合」**，不是單一答案：同音題（`homophone-sc`）允許落在同音群裡的任何一個。

四種代表性列（實際輸出）：

```json
{ "query": "王伟",     "kind": "exact-sc",      "top1": "u001", "top1_score": 1.0,
  "margin": 0.153, "hit1": true,  "hit3": true,  "dept_ok": null,
  "flagged": true,  "why": "homophone-sibling" }

{ "query": "汪伟",     "kind": "homophone-sc",  "top1": "u002", "top1_score": 1.0,
  "margin": 0.153, "hit1": true,  "hit3": true,  "dept_ok": null,
  "flagged": true,  "why": "homophone-sibling" }

{ "query": "王先生",   "kind": "surname-only-sc","top1": "u001", "top1_score": 0.62,
  "margin": 0.0,   "hit1": true,  "hit3": true,  "dept_ok": null,
  "flagged": true,  "why": "low-score" }

{ "query": "帮我接业务", "kind": "dept-only-sc",  "top1": null,   "top1_score": 0.0,
  "margin": 1.0,   "hit1": false, "hit3": false, "dept_ok": true,
  "flagged": null, "why": "dept-only" }
```

判讀兩個眉角：

- **`margin: 0.0` + `hit1: true`** 是正常的（「王先生」兩個王同分，`expect` 含兩個）。
- **`flagged: true` 配 `hit1: true`** 也是正常的（字面命中，但名冊裡有同音兄弟 → 還是要問）。

### 5.2 `out/eval_summary.json`：`n` + 17 個指標

| 指標 | 計算式 | 目前值 | 判讀方向 |
|---|---|---|---|
| `n` | 測資總數 | **342** | — |
| `silent_wrong_rate` | 沒被標記澄清 **且** `hit1` 為 false 的比例（排除 `dept-only-sc`） | **0.000** | **最重要**：這是「安靜地轉錯人」的機率，必須趨近 0 |
| `top1_easy_sc` | `{exact-sc, drop-char-sc, english, filler-sc}` 的 `hit1` 平均 | **1.000** | 越高越好；掉下來代表比對層退步 |
| `top1_easy_tc` | `{exact-tc}` 的 `hit1` 平均 | **1.000** | 繁體對照組，應與 `top1_easy_sc` 一致 |
| `sc_vs_tc_gap` | `exact-sc` 的 `hit1` − `exact-tc` 的 `hit1` | **0.000** | ≠ 0 代表簡繁正規化出問題 |
| `recall3_hard` | `HARD_KINDS`（同音／只講姓／只講名／部門+姓）的 `hit3` | **1.000** | 疑難題「正確的人在不在前三名」 |
| `top1_homophone` | `{homophone-sc}` 的 `hit1` | **0.778** | **不該追求 1.0**：同音題本來就該靠澄清，這裡只確認沒有系統性偏好 |
| `homophone_dept_top1` | `{homophone-dept-sc}` 的 `hit1` | **1.000** | 同音＋報部門時 top-1 應該正確；掉下來代表「部門線索沒被用來解歧義」（`DEPT_SCOPE_MIN` 或部門加權壞了） |
| `dept_acc` | `{dept-only-sc}` 的 `dept_ok` | **1.000** | 只講部門時有沒有轉對 |
| `clarify_rate_hard` | `HARD_KINDS` 被標記澄清的比例 | **0.990** | 越接近 1 越安全（該問的都有問） |
| `clarify_rate_easy` | `STRICT_KINDS`（`exact-sc, exact-tc, english, filler-sc`）被標記澄清的比例 | **0.230** | 使用者體驗成本；**多數是名冊裡真的有同音兄弟，不是誤報** |
| `garbage_false_match` | `{garbage}` 中 `hit1_or_any` 為 true 的比例 | **0.000** | **亂句硬抓到人** —— 比同音字選錯嚴重，必須 0 |
| `nickname_trap_clarify` | `{nickname-trap}` 被標記澄清的比例 | **1.000** | 「王伯伯」這種稱謂要問，不能直接轉 |
| `no_requery_rate` | 跑決策層後，`{surname-only-sc, nickname-trap, drop-char-sc}` 中 `action != ask` 的比例 | **1.000** | 只講姓／只有稱謂／漏字時**不該再追問**（走 `surname-only` / `incomplete-name` fallback 或直接轉） |
| `third_ask_rate` | 第 1 輪追問的題目，在第 2 輪仍為 `ask` 的比例 | **0.000** | 兩輪為上限：第 2 輪仍歧義就 `clarify-exhausted` → fallback |
| `garbage_false_route` | 跑決策層後，`{garbage}` 中 `action ∈ {route, route_dept}` 的比例 | **0.000** | **亂句被轉出去**（轉人或轉部門都算）—— 靠 `has_routing_cue()` 閘門擋住 |
| `dept_route_recall` | `{dept-only-sc}` 中真的有轉到部門的比例 | **1.000** | 確認閘門沒把正常流量也擋掉 |
| `alias_not_found_rate` | 只講名但 alias 沒登記時，`action == not_found` 的比例 | **1.000** | 應為 1：不亂轉、不掛線，走 not-found 到總機 |

`HARD_KINDS` / `STRICT_KINDS` 的定義在 `eval.py` 頂端（`HARD_KINDS`、`STRICT_KINDS` 常數），
`drop-char-sc` 刻意**只算在 `EASY_KINDS`、不算在 `STRICT_KINDS`** —— 少一個字本來就有歧義。

**判讀順序**：先看 `silent_wrong_rate`（安全），再看 `recall3_hard`（能力），
最後才看 `clarify_rate_easy`（體驗）。前者不合格時，後者再漂亮都沒有意義。

### 5.3 門檻掃描（stdout ②）

固定掃 `homophone_floor ∈ {0.45, 0.55, 0.65, 0.75, 0.85}`，每列三欄：

| 欄位 | 意義 |
|---|---|
| `silent_wrong` | 同 `silent_wrong_rate`（全體） |
| `clarify_hard` | 疑難題被追問的比例（越高越安全） |
| `clarify_easy` | 無歧義題被追問的比例（越低體驗越好） |

目前（342 筆）：

```
  floor | silent_wrong | clarify_hard | clarify_easy
   0.45 |        0.000 |        0.990 | 0.230
   0.55 |        0.000 |        0.990 | 0.230
   0.65 |        0.000 |        0.990 | 0.230
   0.75 |        0.000 |        0.990 | 0.230
   0.85 |        0.000 |        0.969 | 0.177
```

**選法**：0.85 的 `clarify_hard` 掉到 0.969（開始漏問疑難題），所以取 **0.65–0.75** 的中段 **0.70**。
`decide()` 實用值是 **0.70**，定義在 `core.HOMOPHONE_FLOOR`（`needs_clarification()` 與
`decide()` 共用同一個常數，沒有第二份數字）。

### 5.4 失敗案例清單（stdout ③）

條件：`kind ∈ HARD_KINDS ∪ {exact-sc, exact-tc}` **且** `hit3 == false`。
輸出格式：

```
  [kind] 'query' -> top1_name (top1_score)
```

**印不出東西是好事**（目前就是空的）—— 代表疑難題與全名題的前三名都沒有落空。
一旦有內容，就是最該先看的 regression：它比 `top1` 掉分更嚴重（正確答案連前三名都進不去）。

<a id="limits"></a>
## 6. 已知限制

- **真實 STT 錯誤率未量測**：8 kHz 窄頻＋台灣國語＋姓名，實際 WER／CER 要靠你自己的錄音。合成測資不回答這個問題。
- **keyterms 未啟用**：ElevenLabs 的 keyterms 可用來 bias 辨識，但模型輸出簡體、
  送繁體 keyterm 可能白費；需要 A/B 驗證（`stt_batch.py --audio-dir … --truth …` 三組共用同一份 truth）。
- **`disclose_names=True` 未完整**：四支固定句未實作、`agi_server.py` 沒有旗標（見 [`sounds.md`](sounds.md)）。
- **STT 全斷沒有 fast-fail**：WS 連不上／中斷且無 transcript → `fallback`／`stream-error` → 總機；
  `result` 端總預算 3 秒（`RESULT_TIMEOUT`），沒有熔斷、沒有指數退避——WS 卡在握手就是等滿
  3 秒才落總機。401／403 與壞 key 一律落總機（2026-11-23 翻案 PoC 的掛線；
  `auth`／`ws` 分界見 [`agi-protocol.md#errors`](agi-protocol.md#errors)）。
- **並發閘 4（寫死）**：四個沒走到 `result` 的 session 在 20 秒 TTL 內佔滿閘，期間新話務
  全量落總機（availability cliff）。
- **TTL 20 秒＝隱私窗口**：OPERATOR／HANGUP／ERROR／閘滿 stub 的 wav 由 sweeper 在 20 秒內刪；
  服務若被 `kill -9`，sweeper 也停止，殘留檔沒有外部清理器。`scripts/agi.sh status` 以
  heartbeat 新舊判 sweeper 死活（超過 5 秒＝DEAD）。
- **故障觀測面**是 `out/calls/*.jsonl` 的 `stt_status`／`stream.status` 與 `scripts/agi.sh status`；
  沒有告警、沒有 metrics 匯出（刻意不做）。
- **同部門同音兄弟**：示範名冊的同音對都刻意跨部門，沒有同部門同音對可測；真機第二場
  遇到同部門同音時行為＝`homophone-unresolvable` → 總機（安全拒猜），合成測資未覆蓋。
- **Go 移植**只有對照表（[`glossary.md#constants`](glossary.md#constants) 是唯一來源），沒有實作。

<a id="regression"></a>
## 7. 回歸與 golden 紀律

- 改 `dialname/`、`eval.py`、`data/people.json` 之後跑 `uv run pytest -q`；`scripts/install-hooks.sh`
  裝的 pre-commit 會在這些路徑 staged 時先跑 L0／L1，非零即拒絕 commit。
- **不要為了讓測試過而放寬門檻**：先判斷是規則錯還是測資錯。
- 刻意改行為 → 用 `scripts/calls.py update` 搬 `expect` 並看 diff，再依 `tests/golden/README.md`
  更新 golden，並在 `CHANGELOG.md` 記一筆。
- 三層回歸（L0 合成／L1 劇本／L2 音檔重播）的分工與 20 通劇本見 [`calls.md`](calls.md) §1；
  開發環境、hook 與測試指令見 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。
- 變更紀錄見 `CHANGELOG.md`；下一步見 `ROADMAP.md`；安全回報見 `SECURITY.md`。
