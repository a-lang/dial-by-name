# AGENTS.md — dial-by-name（語音撥號）

> 給**接手執行的 agent** 的入門脈絡：結構、契約、地雷；細節在 §2 指到的文件。
> 產品說明在 `README.md`；設計與理由在 `docs/design.md`；版本變更在 `CHANGELOG.md`。

## 1. 這是什麼

FreePBX／Asterisk 的「說姓名 → 轉分機」參考實作（Python）。中文姓名經 STT 後常有同音字、
漏字、只講姓、只講稱謂等問題；本專案做拼音比對 + 部門收斂 + 澄清決策，最後產生 Asterisk
可執行的決策。

示範名冊與測資都是**合成資料**；ElevenLabs STT adapter（批次＋Realtime）與 FastAGI
兩相處理器（`start`／`result`）已完成，但真實電話評測不在本 repo 內。

> 架構定案：**AGI 只做 STT＋decide**，收音、播音、轉接、回合全在 FreePBX dialplan
> （`docs/dialplan.md`）。

## 2. 先讀哪份

| 想知道 | 讀 |
|---|---|
| 為什麼這樣設計、指標怎麼算、門檻為何是這些值 | `docs/design.md` |
| 產品定位、部署主線（quickstart）、服務維運、架構、量測 | `README.md` |
| 欄位、列舉值、常數 | `docs/glossary.md` |
| I/O 對映與 Decision → PBX | `docs/io-mapping.md` |
| dialplan 片段、`DBN_*` 變數、失敗分派、按鍵語意、貼上與冒煙；部署主線在 README | `docs/dialplan.md` |
| 20 通劇本協議與判定 | `docs/calls.md` |
| 下一步 | `ROADMAP.md` |
| 這個公開版的來源 | `docs/provenance.md` |

## 3. 結構與契約

```
dialname/core.py      正規化、拼音變體、評分、索引桶、部門解析、needs_clarification（產品邏輯）
dialname/decision.py  本地決策引擎：決策樹 decide()、clarify_plan、姓名音檔命名（clip_hash／name_clip／name_manifest）
dialname/agi.py       AGI 處理器：`start` 登記旁路 session、`result` 等 transcript → decide → DBN_*（`handle_turn` 留給離線工具）＋薄協議層；**不碰電話層**
dialname/stt_client.py ElevenLabs Scribe v2 批次 adapter（wav → Transcript）
dialname/stt_realtime.py ElevenLabs Realtime WS client：PCM 串流／事件解析／錯誤分類
dialname/turns.py     TurnManager：一輪一 session、WAV tail、TTL sweeper 語意
dialname/health.py    sweeper heartbeat／status 判定（`out/agi.health.json`）
dialname/pbx_directory.py 正式名冊入口：userman（join userman_directories）＋aliases.json＋departments.json＋DB 連線
agi_server.py         FastAGI 進入點（threaded，listen 127.0.0.1:4573；SIGHUP 原子重載）
eval.py               342 題指標 + 門檻掃描（argparse；--transcripts/--calls）
trace.py              單句除錯 CLI（有 argparse）
demo.py               9 個通話狀態展示
stt_batch.py          STT 冒煙測試／離線重播（keyterms A/B）
docs/dialplan.md      dialplan 片段、`DBN_*` 變數、電話層失敗分派
docs/calls.md         20 通劇本的撥號協議與人工判定（表格由 `scripts/calls.py table` 產生）
assets/cover.html     專案封面圖（四層 × 五階段；中／英各一版 cover.html、cover-en.html）
scripts/check_sounds.py 檢查 19 個提示音（檔名 ASCII、8k/mono/16-bit）
scripts/check_aliases.py 唯讀檢查 aliases.json（孤兒／重複／空值／控制字元；非零退出碼）
scripts/agi.sh        手動啟停 FastAGI（PoC：foreground/start/stop/reload/status）
scripts/install-hooks.sh 安裝 pre-commit（staged 含 dialname/*.py、eval.py、data/people.json 時先跑 L0/L1）
scripts/calls.py      劇本工具：table／check／compare／truth-out／replay／update（只 import `dialname`）
scripts/jev_probe.py  模糊拉鋸可達面掃描：reason→L1–L4 分佈、L4>0＝重開訊號（JSON 或 `--pbx-db` userman 冊；零 live 呼叫）
data/people.json      40 人 / 8 部門的示範名冊（單一 source_directory；內嵌 3 個 alias：阿明／小美／筱美）
data/departments.json 8 部門定義（userman 模式的 Department[]；dept_id 契約）
data/aliases.example.json  小名 sidecar 範本（正式檔 data/aliases.json 不入版控）
data/call_script.json 20 通劇本的機器判定
tests/                pytest（L0 golden、L1 劇本、compare、replay、update、STT、部門、決策、AGI、pbx_directory、alias 政策、dialplan ASCII、jev_probe）
out/                  eval／通話日誌產物（gitignored）
```

**硬性契約**

1. `eval.py` / `trace.py` / `demo.py` / `agi_server.py` / `stt_batch.py` **只能** import `dialname`
   套件內的模組（`core`、`decision`、`stt_client`、`agi`）；不得自行實作正規化、繁簡轉換、
   同音分群、評分、門檻或決策。要加能力就加到 core。
2. `decide()` 的判斷順序**不可調換**（`docs/design.md` §2 有十條順序與理由；`no-intent` 插在最前面）。
3. **決策層零外部模型存取**：`decide()` 沒有委派參數，`dialname/` 內沒有任何通往外部 API 的路徑。
   `scripts/jev_probe.py scan` 是重開訊號的看護者——(d) 分支（`ambiguous-ask:*`）於全部現有語料
   可達率 **0**（合成 342 題、L1 劇本 20＋6 輪）。此為**結構性證據**（由現行規則定義，
   規則／門檻／名冊變更後須重跑 scan）。**重開條件**（任一即重議，改前在 `CHANGELOG.md` 記錄）：
   ① scan 於真實通話語料掃出 L4；② 正式階段 30 通真實評測出現低分多候選案例；
   ③ 規則變更使 scan 的 L4 脫離 0。重開＝照 `docs/design.md` §4.4 的重建程序做，
   並全部重新評估（不是復活舊檔）。
4. 部門路由鍵是 `dept_id`（ASCII）；中文顯示名只進 log／payload，不進檔名、dialplan、通道變數。

## 4. 環境與指令

執行環境：Linux，Python 3.11，`uv` 管理虛擬環境。

```bash
uv sync                 # 第一次或改過 pyproject 之後
uv run eval.py
uv run trace.py "小明"
uv run demo.py
uv run pytest -q
```

| 事實 | 說明 |
|---|---|
| 套件管理 | `uv`（`uv.lock` 已存在，是依賴唯一來源）；不要用 pip 直接裝 |
| **API key** | `ELEVENLABS_API_KEY`（＋ userman 模式的 `DBN_MYSQL_*`）由 shell 或**專案根目錄**的 `.env`（systemd `EnvironmentFile`）提供；權限 600、不入版控、不進 rsync 樹，也不要寫進指令歷史；`.env` 不要加 `export` |
| 若 key 沒設 | 停下來回報，不要改成假造回應繼續 |
| 虛擬環境 | 用專案的 `.venv`；壞了直接 `rm -rf .venv && uv sync` 重建 |
| 正式依賴共五個 | `pypinyin` / `rapidfuzz` / `opencc-python-reimplemented` / `httpx` / `pymysql`。評估中的新套件先放 `[dependency-groups]` dev，**轉正後移入 dependencies** |

## 5. 地雷（都是踩過的）

1. **合成測資的數字不是準確率**。342 題是對照表模擬同音錯誤，只能當「規則改壞會掉下來」的回歸訊號。
2. **統一用專案的 `.venv`（Python 3.11）**，不要用其他解譯器重建，套件行為會不同。
3. **`trace.py` 的 ⑧ 是決策**（時間軸是 `⑦ clarify → ⑧ decide`）。
4. **只講姓一律 fallback**（`surname-only`），不看候選數，也不看姓氏是否唯一。
5. **同音檢查排在 `low-score` 之前**。原本 `top < 0.92` 會提前 return，所有低分查詢都不檢查同音。
6. **同姓＋名同音不同字**（李佳蓉／李佳容）走 `same_surname_homophone()`，不受分數門檻限制。
7. **`data/people.json` 是示範名冊**，含刻意的同音對；改它會動到所有指標與 20 通劇本的預期分機。
8. **`dept_id` 是 ASCII 鍵**（`^[a-z][a-z0-9_]*$`），同時是音檔尾碼與 dialplan label；中文部門名只放 `name`。
9. **只收 `source_directory` 的人**；`person.directory` 不符或 `dept` 解析不到 → 載入時報錯，不要靜默跳過。
10. **掛線政策**：無意圖／無回應 → `hangup`（先播 `goodbye`）；已表達找人意圖但收斂不了 → 總機。
11. **repo 內沒有音檔**，也沒有 `sounds/` 目錄。決策層的 `sounds/zh/<name>` 只是標籤；
    部署位置是 `/var/lib/asterisk/sounds/dbn/zh/`。要驗音檔請對自己錄的暫存目錄跑 `scripts/check_sounds.py`。
12. **STT 墊字後接標點**：Realtime 常吐「呃......名字」（ASCII 句點 U+002E）；`PUNCT_RE`
    （`core.py:32`）必須涵蓋 ASCII `.`，否則 `_strip_fillers` 剝掉墊字後停在句點，
    姓名檢索全滅（2026-11-25 實撥教訓）。

## 6. 目前狀態

- `uv run pytest -q`：**241 passed**
- `eval.py`：342 題全綠 — `silent_wrong 0.000`、`recall3_hard 1.000`、`homophone_dept_top1 1.000`、
  `clarify_hard 0.990`、`clarify_easy 0.230`、`garbage_* 0/0`、`no_requery 1.000`、`third_ask 0.000`
- 關鍵常數：`WEIGHTS` char .20 / partial .16 / sur .30 / length .14 / given .10 / initials .10；
  `MIN_SCORE .45`、`HOMOPHONE_FLOOR .70`、`CONFIDENT_SCORE .92`、`DEPT_BONUS +.10` / `DEPT_PENALTY −.08`、
  `DEPT_SCOPE_MIN 1`、`SINGLE_CANDIDATE_FLOOR .70`
- 已完成：STT adapter（批次＋Realtime）、FastAGI 兩相協議（`start`／`result`）＋TTL sweeper＋
  Realtime 邊錄旁路、FastAGI server（SIGHUP 原子重載）、pbx_directory（userman＋aliases sidecar）、
  20 通劇本工具鏈、Jev 委派機制移除、19 支提示音的規格與檢查工具
- 真機實撥兩場（2026-11-25）：第一波成功標準 #1 通過（邊際）、`PUNCT_RE` 修正實戰驗證、
  同部門同音＝安全拒猜
- 尚未做：Go 移植、`disclose_names=True` 的四支固定句、keyterms A/B、真實接聽端語料、
  systemd（2026-11-25 裁定不實裝，正式模式＝手動 `scripts/agi.sh`）。外部模型整合已移除，
  (d) 可達率 0 作為重開訊號（契約 3）

## 7. 工作鐵則

- 改 `core.py` 的評分／門檻／順序前**先問**；改完必須跑 `eval.py` 並確認輸出可解釋。
- 改 `dialname/`、`eval.py`、`data/people.json` 後必跑 `uv run pytest -q`，綠了才 commit
  （pre-commit hook 會擋：staged 含這些路徑時先跑 L0／L1，非零即拒絕；`--no-verify` 要在
  `CHANGELOG.md` 記原因）。
- **不要**為了讓測試過而放寬門檻；先確認是規則錯還是測資錯。
- **不要把 key 寫進程式碼或版控**；本機 shell 或 PBX 的 `.env`（systemd `EnvironmentFile`）可以，
  檔案權限 600、不入版控。
- 文件與程式要同步：改行為就改 `README`／`docs/design.md`，並在 `CHANGELOG.md` 記一筆。
- **文件指令引用的每個路徑／檔案，必須由更早的步驟產生或明確標注前提；不得憑空出現**。
- 交付時附上**實際執行輸出**（不是「應該會過」）。
