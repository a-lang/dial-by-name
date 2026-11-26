# 架構：分層、AGI 兩相與狀態邊界

本檔說明一通電話在哪些層之間流動、狀態放在哪裡，以及為什麼 AGI 只做 STT 與決策。

邊界只有兩條：**AGI 只做 STT＋決策**、**電話層只認 `DBN_*`**。收音、播音、轉接與回合計數
全在 dialplan，AGI 在一輪之內保存 session，跨輪狀態只有一處。

## 四層

| 層 | 負責 | 程式 |
|---|---|---|
| 電話層 | 收音、播音、轉接、回合（`DBN_ATTEMPT`／`DBN_TRIES`）、失敗分派 | `[dbn-main]`／`[dbn-play-speech]`（[`dialplan.md#snippet`](dialplan.md#snippet)） |
| AGI 服務 | AGI 協議、session 池、TTL、health、SIGHUP 重載 | `agi_server.py`、`dialname/agi.py`、`dialname/turns.py`、`dialname/health.py` |
| 決策核心 | 正規化、拼音／模糊音、評分、收斂、決策樹 | `dialname/core.py`、`dialname/decision.py` |
| 名冊 | 人員與部門的來源；正式版讀 userman | `dialname/core.load_directory()`、`dialname/pbx_directory.py` |

```
來電 ──► FreePBX dialplan（收音／播音／轉接／回合，全在 dialplan）
             │  Record → processing
             ▼
        FastAGI 127.0.0.1:4573        ── agi_server.py（threaded，SIGHUP 原子重載）
             │
             ▼
        AGI start / result   ── dialname/agi.py（兩相）
             │  start：旁路 wav 登記 session，立即返回（不設通道變數）
             │  result：① tail 旁路 wav → Realtime STT（ElevenLabs）→ 逐字稿
             │  ② core：正規化 → 拼音 → 模糊音 → top-8 候選（1–3 ms）
             │  ③ decision.decide()：轉／問一句／交總機
             ▼
        DBN_ACTION / DBN_REASON / DBN_TARGET / DBN_DEPT / DBN_SPEECH / DBN_CANDS
             │
             ▼
        dialplan 依 DBN_* 動作：Dial(Local/…@from-internal) 或 播下一句
```

五個階段（收音・轉錄・收斂・決策・執行）逐段的輸入輸出見
[`agi-protocol.md`](agi-protocol.md) §1——那張表是本專案的 I/O 邊界，不在這裡重複。

## AGI 兩相：`start` 登記、`result` 收工

| 動詞 | 時機 | 做什麼 | 回傳 |
|---|---|---|---|
| `start` | `Record()` 之前 | 登記 `(call_id, seq)` → 旁路 wav 路徑，開 Realtime 串流 | **不設任何通道變數**（dialplan 每輪已預設 `DBN_ACTION=fallback`） |
| `result` | `Playback(processing)` 之後 | 取 transcript → `decide()` → 六個 `DBN_*`，刪兩支 wav | 六個通道變數（空值也送） |

`start` 立即返回，所以收音期間 dialplan 不必等 AGI；決策與六個 `DBN_*` 全在 `result`。
參數、回傳與 `Decision` → PBX 對映見 [`agi-protocol.md`](agi-protocol.md)。

失敗時 dialplan 還有安全網：`AGISTATUS != SUCCESS` → 總機；`start` 崩潰或服務重啟導致
`result` 找無 session，同樣 `fallback` → 總機（不是掛線）。

## 一輪一 session：`TurnManager`

`dialname/turns.py` 是「通話中的暫存區」，不是對話記憶。

| 常數 | 值 | 作用 |
|---|---|---|
| `MAX_SESSIONS` | **4**（寫死） | 並發閘。四個沒走到 `result` 的 session 在 TTL 內佔滿閘，期間新話務全落總機（stub session → `fallback`） |
| `TTL_SECONDS` | **20.0** | `result` 沒來的分支（OPERATOR／HANGUP／ERROR／閘滿）由 sweeper 到期清理 |
| `COMMIT_WAIT` | 1.0 | EOF 補 commit 後等最後一則 transcript 的上限 |
| `RESULT_TIMEOUT` | 3.0 | `result` 端總預算，與 tailer／WS 的 EOF 收尾並行，不是串行疊加 |

- 一輪 = 一個 session（key `call_id`＋`seq`）；`attempt` 1→2 是**新的一輪**，不是同一個 session。
- 每個 session 登記**兩支 wav**：`MixMonitor` 旁路 `-in.wav` 與 `Record` 主檔 `.wav`；
  走 `result` 就刪，沒走到由 sweeper 在 TTL 到期時刪。
- session 池**跨 SIGHUP 存活**：`reload` 只換名冊索引，進行中的通話不重置。
- sweeper 每 1 秒寫 `out/agi.health.json`（原子換檔）；heartbeat 超過 5 秒沒更新＝`sweeper: DEAD`，
  `scripts/agi.sh status` 回非零。判定細節見 [`operations.md#health`](operations.md#health)。

## 名冊兩個入口，同一個形狀

| 入口 | 來源 | 用途 |
|---|---|---|
| `core.load_directory()` | `data/people.json`（40 人／8 部門示範名冊） | 離線開發、測試、`eval.py` |
| `pbx_directory` | FreePBX userman＋`data/aliases.json`＋`data/departments.json` | 正式部署（`AGI_PBX_DB=1`＋`AGI_SOURCE_DIRECTORY`） |

契約與 JSON 版相同：下游（`name_match_keys()`、`alias-ask`、`alias-not-found`）不知道資料
從哪來，所以切換與回滾都只是換入口。只收 `source_directory` 指定的一個 userman directory
的人（`auth` 不符者不進索引），避免跨目錄重複與測試目錄混入電話簿。

**部門路由鍵是 `dept_id`（ASCII，`^[a-z][a-z0-9_]*$`）**：同時是 queue 對映鍵、dialplan
label `dept_<id>` 與音檔尾碼 `dept-<id>`；中文部門名只進 log／payload，不進檔名、dialplan、
通道變數。部門定義（`Department[]`）只有 `data/departments.json` 一個家，userman 只有人的
自由文字 `department`。

`data/aliases.json` 是小名 sidecar（key 是 `userman_users.id`，不是分機）；孤兒 `user_id`
會讓 reload **失敗並保留舊索引**。欄位、驗證與政策見
[`decision-logic.md#alias`](decision-logic.md#alias)。

## 隱私模型

<a id="privacy"></a>

電話簿是「撥得到就能查」的服務。只要開口念出名字，就等於提供列舉介面——換 20 個姓氏
就能蒐集全公司名單。預設 `disclose_names=False` 下三條規則：

| 規則 | 做法 |
|---|---|
| 不念名字 | 澄清一律播「請說全名」；轉接只播「為您轉接」 |
| 不確認存在 | 回覆內容與「這個人存不存在」無關，避免變成 oracle |
| 不列舉數量 | 不說「有 2 位符合」 |

**按鍵只用來選「部門／總機」，永遠不用來盲選人**：來電者若分得出是哪一位就不需要澄清；
分不出來，更不可能知道 1 和 2 各是誰。`Decision.label` 保留姓名，但只給 log 用，不進 `speech`。

- 唯一的第三方是 STT（ElevenLabs Scribe v2），它聽到的本來就是來電者的語音；決策 100% 本地，
  整本電話簿不出機器。
- `recordings/` 的暫存 wav 在 `result` 即刪，其餘由 sweeper 在 **TTL 20 秒**內清；
  `kill -9` 後沒有外部清理器（已知限制）。
- `data/aliases.json` 與 `sounds/zh/manifest.json` 含真名：不入版控、權限 600、不對外 serve；
  `data/aliases.example.json` 才是進版控的範本。
- `disclose_names=True` 需要的四支固定句（`q-is-it`／`q-or`／`q-question`／`transferring-name`）
  與逐人名音檔**未實作**，`agi_server.py` 也沒有旗標；那是內線專用部署的工作，不在本 repo。

## 檔案佈局

```
dialname/core.py           正規化、拼音變體、模糊音、評分、索引桶、部門解析、澄清判斷
dialname/decision.py       決策樹 decide()、clarify_plan、姓名音檔命名（clip_hash／manifest）
dialname/agi.py            AGI 兩相處理器（start／result）＋薄協議層（不碰電話層）
dialname/stt_client.py     ElevenLabs Scribe v2 批次 adapter（wav → Transcript）
dialname/stt_realtime.py   ElevenLabs Realtime WS client（PCM 串流／事件解析／錯誤分類）
dialname/turns.py          TurnManager：一輪一 session、WAV tail、TTL sweeper
dialname/health.py         sweeper heartbeat／status 判定（out/agi.health.json）
dialname/pbx_directory.py  正式名冊入口：userman＋aliases.json＋departments.json
agi_server.py  eval.py  trace.py  demo.py  stt_batch.py
docs/deploy.md             部署主線（clone、目錄、.env、音檔、上線、更新）
docs/operations.md         上線後維運（啟停、health、名冊異動、故障演練、systemd）
docs/architecture.md       本檔：分層、兩相、session、名冊、隱私
docs/decision-logic.md     不變式、決策順序、評分門檻、alias 政策、外部模型
docs/agi-protocol.md       五階段 I/O、start／result、DBN_*、錯誤分類、JSONL
docs/dialplan.md           dialplan 片段、DBN_* 變數、電話層失敗分派
docs/glossary.md           欄位／列舉值字典、常數表
docs/sounds.md             19 支台詞、格式、命名與 hash
docs/evaluation.md         指標、效能、合成資料警語、限制、golden 紀律
docs/calls.md              20 通劇本協議與判定（表格由 scripts/calls.py table 產生）
data/people.json           40 人／8 部門的合成示範名冊
data/departments.json      8 部門定義（dept_id 契約）
data/call_script.json      20 通劇本的機器判定
scripts/                   agi.sh／calls.py／check_sounds.py／check_aliases.py／jev_probe.py／install-hooks.sh
tests/                     pytest：L0 golden、L1 劇本、AGI、Realtime、名冊、alias 政策…
```

## 設計取捨

- **跨輪狀態只有一處**：回合計數在 dialplan 的通道變數，AGI 不保存對話；AGI 壞掉時
  `DBN_ACTION=fallback` 的預設值讓通話仍落到人。
- **決策層零外部呼叫**：`decide()` 沒有委派參數，`dialname/` 內沒有任何通往外部 API 的路徑；
  重開條件見 [`decision-logic.md`](decision-logic.md) §6。
- **兩相而不是一相**：`start` 立即返回，收音期不等 AGI；Realtime 串流在收音同時跑，
  常態輪的 transcript 在 `Record()` 結束前已 commit，`result` 的等待接近 0。
