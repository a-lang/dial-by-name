# AGI 契約：輸入、輸出與邊界

本檔定義 AGI 的參數、回傳與錯誤分類，以及 `Decision` 到 PBX 的對映。

I/O 契約只回答一件事：**什麼進去、什麼出來、在哪一層發生**。所有數值都是
`data/people.json`（40 人）實際跑出來的，可用 `uv run eval.py` 重現。

## 1. 五個階段（每段都有明確的輸入與輸出）

```
① 名冊       userman_users / people.json     →  Person[] + Department[]
② 正規化     逐字稿 str                       →  q_name str + Department|None
③ 收斂        q_name                          →  Candidate[] (≤ top_k)
④ 決策        Candidate[] + utterances        →  Decision
⑤ 執行        Decision                        →  PBX 動作（Goto / Playback）
```

| # | 函式 | 輸入 | 輸出 | 不變量 |
|---|---|---|---|---|
| ① | `load_directory()`／`load_from_pbx()` | `data/people.json`／`userman_users` | `Directory`（內含 `Person[]`、`Department[]`、首音節桶） | 每人預先算好 `name_key` / `variants` / `fuzzy_variants` / `initials`；userman 的 `fname`／`lname` 任一缺 → 跳過（`displayname` 不當名稱來源） |
| ② | `normalize()` → `strip_noise()` → `dept_match()` | 逐字稿 `"我要找業務部的王先生"` | `q_name="王"`, `dept=<業務部>` | 順序不可換：先剝贅詞，再找部門；字面沒中時再用整串拼音比對（`人資部`→`人之不`） |
| ③ | `Directory.search()` | `q_name` + `dept` | `[Candidate(person, score, reasons)]`，分數降冪 | `score ≥ 0.45`（單字查詢放寬到 0.30；否則是「財務部的張先生」在部門內拿到 0.447 被砍光 → 誤轉部門 queue） |
| ④ | `decide()` | `utterances[]` + `Candidate[]` + `dept` + `attempt` | `Decision` | `action ∈ {route, route_dept, ask, fallback, hangup}`；順序見 [`decision-logic.md#order`](decision-logic.md#order) |
| ⑤ | dialplan | `Decision` | dialplan / 播放 / 掛斷 | 只認 `action` 與 `target`，其餘不影響 PBX |

### 型別

```python
Person(pid, name, directory, dept, title, extension, aliases)  # 名冊一列
Department(dept_id, name, aliases)                        # 部門 → ASCII 路由鍵
Candidate(person: Person, score: float, reasons: list[str])
Decision(action, target, label, reason, speech: list[str], confidence, options)
```

欄位與列舉值見 [`glossary.md`](glossary.md)；部門路由鍵 `dept_id` 的契約見
[`architecture.md`](architecture.md)。

## 2. `start`：只登記，不設變數

| 位置 | 參數 | 內容 |
|---|---|---|
| `agi_arg_1` | 動詞 | `start` |
| `agi_arg_2` | `side_wav` | `MixMonitor` 旁路檔（`${DBN_WAV}-in.wav`） |
| `agi_arg_3` | `call_id` | `${UNIQUEID}` |
| `agi_arg_4` | `seq` | `${DBN_SEQ}`（每輪 +1） |

回傳 **None**：不寫任何通道變數。理由：dialplan 每輪開頭已設 `DBN_ACTION=fallback`，
分派鏈在 `result` 之後，`start` 寫的東西只會被覆寫；閘滿或內部失敗時由 `TurnManager`
建 stub session，路徑照登記，讓 `result` 或 sweeper 清檔。

## 3. `result`：取 transcript → `decide()` → 六個 `DBN_*`

| 位置 | 參數 | 內容 |
|---|---|---|
| `agi_arg_1` | 動詞 | `result` |
| `agi_arg_2` | `wav` | `Record()` 主檔（`${DBN_WAV}.wav`） |
| `agi_arg_3` | `call_id` | `${UNIQUEID}` |
| `agi_arg_4` | `seq` | `${DBN_SEQ}` |
| `agi_arg_5` | `attempt` | `${DBN_ATTEMPT}`（1→2；非數字視為 1） |
| `agi_arg_6` | `DBN_CANDS` | 上一輪候選 `pid:score&…`；可空 |

一次 `result` 只處理**一輪**。六個通道變數（空值也送，一律加引號；`SET VARIABLE` 要求
`argc == 4`，空值不給引號會 520 語法錯誤）：

| 變數 | 意義 | 例 |
|---|---|---|
| `DBN_ACTION` | 本輪動作；**六值**：`route`／`route_dept`／`ask`／`retry`／`fallback`／`hangup` | `route` |
| `DBN_TARGET` | `route` 是分機號、`route_dept` 是 `dept_id`；其餘空 | `2101`／`sales` |
| `DBN_DEPT` | 只有 `route_dept` 時等於 `dept_id`；用來查 queue 與播 `dept-<id>` | `sales` |
| `DBN_SPEECH` | 依序播放的 clip 名，`&` 分隔；空＝不說話 | `transferring&dept-sales` |
| `DBN_REASON` | 決策理由（[`glossary.md`](glossary.md) §6）或傳輸層理由 | `direct:confident` |
| `DBN_CANDS` | 候選 `pid:score&…`，給**下一輪**收斂用 | `u001:1.0&u002:0.85` |

`DBN_CANDS` 是跨輪唯一的狀態載體：第二輪只講部門時，`result` 用它把上一輪候選收斂到
該部門（剩 1 人就直接轉）；沒有交集才 `dept-only` 轉整個部門。值一律經 `_ascii_result()`
檢查（不含控制字元、`"`、`\`），不合格 → `fallback`／`non-ascii`。

## 4. `Decision` → PBX 動作

| `action` | PBX 動作 | `target` 的意義 | `speech` |
|---|---|---|---|
| `route` | `Dial(Local/<target>@from-internal)` | 分機號 | 「為您轉接」（**不念姓名**） |
| `route_dept` | 依 `target`（`dept_id`）查 dialplan 的 queue／ringgroup | `sales`（dept_id） | 轉接前播報（`transferring`＋`dept-<id>`） |
| `ask` | `Playback(speech…)` → 重新錄音 → 回到 ② | 無 | 澄清／重問；`options` 只給 log，電話層不消費 |
| `fallback` | 轉總機（已表達找人意圖但收斂不了） | 由 dialplan 的 `DBN_OPERATOR` 提供 | `not-found`（服務故障時空） |
| `hangup` | `Playback(goodbye)` → `Hangup()`（無意圖／無回應） | 無 | `goodbye`（謝謝您的來電） |

一個通話最多 2 輪（`attempt` 1→2）。**AGI 本身不保存跨輪狀態**：每輪只傳當輪逐字稿
（`decide()` 只讀 `utterances[-1]`）；狀態由 dialplan 用通道變數 `DBN_CANDS`
（`pid:score&…`）帶回下一輪，AGI 只把它當參數收。

**只講名／小名走 alias 白名單**（2026-11-16 定案）：查詢首字不是名冊裡的姓時，姓名模糊
比對不可靠——實撥「美玲」被 STT 聽成「梅林」後，以 0.4816 命中「林志明」並直接轉出去。
這條路只比對 `aliases`（字面或同音，中英皆可）：唯一命中 → `route`；多人 → `ask`
（`alias-ask`）；查無但發音像某人的名 → `fallback`（`alias-not-found`）→ 總機。
亂句（不像任何人的名）仍走 `no-intent` → 掛線，不會被送進總機。

## 5. 實測對映（10 句，涵蓋所有分支）

| 輸入（逐字稿） | 正規化 | 部門 | top-2 候選(分數) | 需澄清 | 決策 | 目標 | speech | options |
|---|---|---|---|---|---|---|---|---|
| `我要找业务部的王先生` | `王` | 業務部 | 王偉 0.72 / 王小明 0.72 | N(surname-only) | **fallback** | — | — | — |
| `王伟` | `王偉` | — | 王偉 1.00 / 汪偉 0.85 | Y(homophone-sibling-ask) | **ask** | — | `q-which-dept` | `0→operator` |
| `章家豪` | `章家豪` | — | 章家豪 1.00 / 張家豪 0.90 | Y(homophone-sibling-ask) | **ask** | — | `q-which-dept` | `0→operator` |
| `张佳豪` | `張佳豪` | — | 張家豪 0.88 / 章家豪 0.79 | Y(homophone-sibling-ask) | **ask** | — | `q-which-dept` | `0→operator` |
| `Amy` | `Amy` | — | 陳艾美 0.95 | N(direct:confident) | **route** | `2107` | `transferring` | — |
| `转业务部` | （空） | 業務部 | — | N(dept-only) | **route_dept** | `sales` | `transferring, dept-sales` | — |
| `帮我接人资` | （空） | 人資部 | — | N(dept-only) | **route_dept** | `hr` | `transferring, dept-hr` | — |
| `找一个很奇怪的人` | `很奇怪人` | — | — | Y(no-candidate-retry) | **ask** | — | `retry` | `0→operator` |
| `我要找财务部的张先生` | `張` | 財務部 | 章家豪 0.32 | N(surname-only) | **fallback** | — | — | — |
| `业务部的王伟` | `王偉` | 業務部 | 王偉 1.00 / 王小明 0.77 | N(direct:confident) | **route** | `2101` | `transferring` | — |

注意後三欄的分工：**決策決定「做什麼」，`target` 決定「去哪裡」，`speech` 決定「說什麼」**，
三者互不推導。

<a id="errors"></a>
## 6. STT 錯誤分類：`auth` 與 `ws`

ElevenLabs Realtime 對壞 key 先在 WS 事件層送 `auth_error`（HTTP 已 101），程式記
`stream.error=ws`／`stt_status=ws`，`stream.detail` 含 `auth_error`；握手層 401／403
或缺少 `ELEVENLABS_API_KEY` 才記 `auth`。**兩者行為相同＝`fallback` → 總機**，差別只在
JSONL 的 `stream.error`／`stream.detail`（2026-11-25 裁定維持現狀；電話層行為面摘要見
[`dialplan.md`](dialplan.md) §3.9）。

| `stt_status` | 觸發 | 決策 |
|---|---|---|
| `auth`／`unavailable`／`ws`／`dropped`／`timeout`／`internal` | WS 連不上／401／403／中斷且無 transcript | `fallback` → 總機 |
| `ok`（`stream.status=dropped`） | WS 中斷但已有 transcript | 用現有文字照常 `decide()` |
| `frames-0` | 旁路檔沒出現／tailer 死 | `fallback` → 總機 |
| `no-session` | `result` 找無 session（`start` 崩潰／服務重啟） | `fallback` → 總機 |
| `stub` | 並發閘滿 | `fallback` → 總機 |
| `no-speech` | 無 commit 且全程無 partial | `retry`（`DBN_TRIES` 用盡由 dialplan 掛線） |
| `partial-only` | 無 commit 但曾有 partial | `fallback` → 總機 |
| `empty` | 有 commit、文字為空 | `retry` |
| `exception` | 未預期例外 | `fallback` → 總機 |

判別訊號是 **VAD partial，不是 frames**：旁路是 audiohook 連續串流，純靜音也會讓
`frames > 0`；`frames` 只用來抓「檔案沒出現」這種系統故障，且只能在 `result` 判定
（`StopMixMonitor` 後必定至少 flush 一次）。

## 7. 每輪 JSONL（`out/calls/YYYYMMDD.jsonl`）

一行一輪，`AGI_LOGS` 決定目錄。這是本檔的單一定義；指標怎麼從它算出來見
[`evaluation.md`](evaluation.md)。

| 欄位 | 型別 | 意義 |
|---|---|---|
| `ts` | str | ISO 時間 |
| `call_id` | str | `${UNIQUEID}` |
| `attempt` | int | 1／2；原值不是數字時另附 `attempt_raw` |
| `seq` | int | 輪次序號（`result` 才有） |
| `wav` | str | 主檔路徑（**join key**；檔案本身已刪，名稱保留） |
| `stt.text` / `.lang` / `.ms` | str／str／int | 逐字稿、語言、等待毫秒（成功標準 #1 的量測點） |
| `stream.status` / `.open_ms` / `.commit_ms` | str／int／int | Realtime 串流狀態與耗時 |
| `stream.frames` / `.partials` | int／int | 旁路影格數／VAD partial 次數 |
| `stream.bypass` / `.error` / `.detail` | str／str／str | 旁路檔、錯誤分類（`auth`／`ws`…）、細節 |
| `candidates[]` | `[{pid, score}]` | 前 5 名候選（`result` 時＝帶回去的 `DBN_CANDS`） |
| `decision.action` / `.target` / `.reason` / `.speech` | str／str?／str／str[] | 決策結果（`offline` 路徑另有 `.confidence`） |
| `stt_status` | str | §6 的分類 |
| `carried_from_prev` / `dept_ignored` | bool／str | 只在發生時出現：候選是上一輪經部門過濾來的／這句部門被當成澄清答案且無交集 |

## 8. 對映不是嚴格 1:1 的地方

| 關係 | 說明 |
|---|---|
| 逐字稿 → 候選 **1:N** | 一句話收斂出最多 `top_k` 個候選（預設 20，決策只用前幾個） |
| 候選 → 澄清句 **N:1** | 預設只播一句「請說全名」（不列姓名）；只有 `disclose_names=True` 時才退化成「N 選 1」（同姓或同音、且 ≥ top×0.8） |
| `DBN_ACTION` 六值 → `Decision.action` 五值 | `retry` 是**傳輸層**值（空逐字稿／STT 失敗／Realtime 無 commit 且無 partial＝沉默），不在 `Decision.action` 裡；由 `handle_result()`（生產）或 `handle_turn()`（離線工具）在呼叫 `decide()` 之前直接產生，上限由 dialplan 的 `DBN_TRIES` 決定（見 [`dialplan.md`](dialplan.md) §3.9） |
