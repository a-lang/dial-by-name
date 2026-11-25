# 輸入 / 輸出對映（I/O Contract）

> 角色：本檔是「JSONL 欄位、`DBN_*` 變數、Decision→PBX 對映（含 STT 錯誤分類 auth/ws）」的唯一來源；跨檔只指向、不複製。

I/O 契約只回答一件事：**什麼進去、什麼出來、在哪一層發生**。所有數值都是
`data/people.json`（40 人）實際跑出來的，可用 `uv run eval.py` 重現。

---

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
| ③ | `Directory.search()` | `q_name` + `dept` | `[Candidate(person, score, reasons)]`，分數降冪 | `score ≥ 0.45`（單字查詢放寬到 0.30） |
| ④ | `decide()` | `utterances[]` + `Candidate[]` + `dept` + `attempt` | `Decision` | `action ∈ {route, route_dept, ask, fallback, hangup}` |
| ⑤ | 呼叫端（AGI/ARI） | `Decision` | dialplan / 播放 / 掛斷 | 只認 `action` 與 `target`，其餘不影響 PBX |

### 型別

```python
Person(pid, name, directory, dept, title, extension, aliases)  # 名冊一列
Department(dept_id, name, aliases)                        # 部門 → ASCII 路由鍵
Candidate(person: Person, score: float, reasons: list[str])
Decision(action, target, label, reason, speech: list[str], confidence, options)
```

---

## 2. 實測對映（10 句，涵蓋所有分支）

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

注意最後三欄的分工：**決策決定「做什麼」，`target` 決定「去哪裡」，`speech` 決定「說什麼」**，三者互不推導。

---

## 3. `Decision` → PBX 動作

| `action` | PBX 動作 | `target` 的意義 | `speech` |
|---|---|---|---|
| `route` | `Dial(Local/<target>@from-internal)` | 分機號 | 「為您轉接」（**不念姓名**） |
| `route_dept` | 依 `target`（`dept_id`）查 dialplan 的 queue／ringgroup | `sales`（dept_id） | 轉接前播報（`transferring`＋`dept-<id>`） |
| `ask` | `Playback(speech…)` → 重新錄音 → 回到 ② | 無 | 澄清／重問；`options` 只給 log，電話層不消費 |
| `fallback` | 轉總機（已表達找人意圖但收斂不了） | 由 dialplan 的 `DBN_OPERATOR` 提供 | 無 |
| `hangup` | `Playback(goodbye)` → `Hangup()`（無意圖／無回應） | 無 | `goodbye`（謝謝您的來電） |

一個通話最多 2 輪（`attempt` 1→2）。**AGI 本身不保存跨輪狀態**：每輪只傳當輪逐字稿
（`decide()` 只讀 `utterances[-1]`）；狀態由 dialplan 用通道變數 `DBN_CANDS`
（`pid:score&…`）帶回下一輪，AGI 只把它當參數收。
第 1 輪候選 ≥2 → 問部門；第 2 輪若只講部門（「客服部」），就用 `DBN_CANDS` 把上一輪候選
收斂到該部門（剩 1 人 → 直接轉）。沒有交集時才 `dept-only` 轉整個部門；
第 2 輪仍 ≥2 → `clarify-exhausted` → fallback。

**只講名／小名走 alias 白名單**（2026-11-16 定案）：查詢首字不是名冊裡的姓時，姓名模糊
比對不可靠——實撥「美玲」被 STT 聽成「梅林」後，以 0.4816 命中「林志明」並直接轉出去。
這條路只比對 `aliases`（字面或同音，中英皆可）：唯一命中 → `route`；多人 → `ask`
（`alias-ask`）；查無但發音像某人的名 → `fallback`（`alias-not-found`）→ 總機。
亂句（不像任何人的名）仍走 `no-intent` → 掛線，不會被送進總機。

**STT 失敗的 auth 映射（2026-11-25 裁定維持現狀）**：ElevenLabs Realtime 對壞 key 先在 WS
事件層送 `auth_error`（HTTP 已 101），程式記 `stream.error=ws`／`stt_status=ws`，
`stream.detail` 含 `auth_error`；握手層 401／403 或缺少 `ELEVENLABS_API_KEY` 才記 `auth`。
兩者行為相同＝`fallback` → 總機，差別只在 JSONL 的 `stream.error`／`stream.detail`。
`docs/dialplan.md` §3.9 另有行為面摘要。

---

## 4. 對映不是嚴格 1:1 的地方

| 關係 | 說明 |
|---|---|
| 逐字稿 → 候選 **1:N** | 一句話收斂出最多 `top_k` 個候選（預設 20，決策只用前幾個） |
| 候選 → 澄清句 **N:1** | 預設只播一句「請說全名」（不列姓名）；只有 `disclose_names=True` 時才退化成「N 選 1」（同姓或同音、且 ≥ top×0.8） |
| `DBN_ACTION` 六值 → `Decision.action` 五值 | `retry` 是**傳輸層**值（空逐字稿／STT 失敗／Realtime 無 commit 且無 partial＝沉默），不在 `Decision.action` 裡；由 `handle_result()`（生產）或 `handle_turn()`（離線工具）在呼叫 `decide()` 之前直接產生，上限由 dialplan 的 `DBN_TRIES` 決定（見 `docs/dialplan.md` §3.9） |

---

## 5. 實作期間修掉的問題

1. **無限遞迴**：`_search()` 用物件 identity（`candidates_pool is not people`）判斷要不要回退全掃，
   但部門限縮每次都會產生新 list → 永遠判定「需要回退」→ 遞迴直到爆掉。
   改成顯式旗標 `_fallback`。
2. **單字查詢被誤判成「沒聽懂」**：「財務部的張先生」在部門內拿到 章家豪 0.447，
   低於 `MIN_SCORE=0.45` 被砍光，結果轉到財務部 queue。改成單字查詢門檻 0.30。
3. **澄清句摻雜無關候選**：「財務部的張先生」原本會問「是章家豪還是曾雅雯？」（曾只是同部門雜訊）。
   改成只列同姓或同音、且分數 ≥ top×0.8 的候選；只有一個時自動退化為「請問是章家豪嗎？」。
4. **`Decision` 欄位順序位移**：新增 `options` 時插在 `confidence` 前面，導致所有用位置傳參的
   `Decision(...)` 把分數塞進 `options`（輸出變成 `options: 1.0`）。修法是把新欄位放**最後**，
   並改用關鍵字傳參。
5. **`flagged` 對 `dept-only` 沒有意義**：`needs_clarification([])` 一律回 `True`，
   讓「只講部門」的列看起來像「該追問」。改成 `null` + `why="dept-only"`。
