# dial-by-name：說姓名 → 轉分機（中文姓名的同音消歧參考實作）

> 角色：本檔是「從零到撥通的主線順序、服務啟停（手動＝正式＋systemd 選用）與本機開發工具」的唯一來源；跨檔只指向、不複製。

FreePBX／Asterisk 用的「姓名 → 分機」比對層。來電者說出名字，系統撥接到對應分機，
**不播報號碼、不念姓名**。

這是一份**可執行的參考實作＋量測腳本**，不是產品：目的是把設計決策變成可驗證的數字。
目前只涵蓋中文姓名（繁體正規化＋漢語拼音＋模糊音）；package 名稱不綁語言，
新增語系時在 `normalize()`／`pinyin_variants()` 的 hook 上分派即可。

授權：AGPL-3.0-or-later（見 `LICENSE`）。

---

## 文件地圖

| 想知道 | 讀 |
|---|---|
| 從零到能撥通（主線順序） | 本檔「快速開始」 |
| PBX 部署、音檔、驗收、人員異動 | `docs/dialplan.md` |
| 設計理由、門檻依據 | `docs/design.md` |
| 名詞、欄位、常數 | `docs/glossary.md` |
| JSONL／`DBN_*`／決策→dialplan 對映 | `docs/io-mapping.md` |
| 20 通劇本協議 | `docs/calls.md` |
| 公開版來源 | `docs/provenance.md` |

---

## 快速開始：從零到撥通

主線只有順序，細節都在指到的章節；沒綠的決策層不要接電話。

1. `uv sync` — 建立本機環境（依 pyproject＋uv.lock）。
2. `uv run pytest -q && uv run eval.py` — L0／L1 與 342 題指標先綠，沒綠別碰電話。
3. 部署樹 rsync 到 PBX（不含 `.venv`／`out`／`.env`／`recordings`）→ `docs/dialplan.md` §1
4. 建 `recordings/`、`out/calls/` 並做權限 probe → `docs/dialplan.md` §1
5. 提示音 19 支放上 `/var/lib/asterisk/sounds/dbn/zh/` → `docs/dialplan.md` §1
6. `.env`（`ELEVENLABS_API_KEY` 等；600）→ `docs/dialplan.md` §1.1
7. 貼 `[dbn-main]`／`[dbn-play-speech]` dialplan＋reload → `docs/dialplan.md` §2
8. `scripts/agi.sh start`＋金絲雀驗證 → 本檔「服務管理與開機自啟」
9. 冒煙一通：先跑 §3.8 的 STT／決策管線檢查，再以 Local channel 撥入 `dbn-main` 演練 → `docs/dialplan.md` §3.8

之後：20 通劇本照 `docs/calls.md` 撥與判；人員異動 SOP 在 `docs/dialplan.md` §3.5／§4。

### 本機工具

```bash
uv sync                                        # 建立 .venv 並安裝依賴（依 pyproject.toml + uv.lock）
uv run eval.py                                 # 342 題合成測資：16 項指標＋門檻掃描
uv run trace.py "我要找業務部的王先生"          # 單句除錯：逐層中間值＋分數拆解＋決策
uv run demo.py                                 # 9 個通話狀態 → 決策
uv run pytest -q                               # L0 golden＋L1 劇本＋單元測試
uv run scripts/calls.py check                  # 只跑 L1（18 通 auto＋3 個 l1_only）
```

不用手動 activate：`uv run` 會自動同步依賴。`requires-python = ">=3.10"`；
主機沒有 3.10+ 時 uv 會自己抓一個。`out/`、`.venv/` 已在 `.gitignore`。

看網路上的東西之前不需要任何 key；**只有真的呼叫 STT 才需要 `ELEVENLABS_API_KEY`**
（`.env`，權限 600、不入版控），userman 模式另外需要 `DBN_MYSQL_*` 四個唯讀連線變數。

---

## 服務管理與開機自啟

### 現狀

正式運行模式＝手動 `scripts/agi.sh`（2026-11-25 裁定：不實裝 systemd unit）。
**重開機不會自動起服務**；開機後需手動 `scripts/agi.sh start`。

### 手動方式（正式模式）

```bash
# 啟動／停止／重啟（.env 已固化 userman 設定：AGI_PBX_DB=1、AGI_SOURCE_DIRECTORY="Test Directory"）
scripts/agi.sh start
scripts/agi.sh restart       # 改程式碼、換 AGI_LOGS、改 .env 後用
scripts/agi.sh reload        # SIGHUP：原子重載名冊（userman／aliases.json 異動後用；不換程式碼）
scripts/agi.sh stop

# 狀態（可腳本判定：running 但 sweeper 不健康 → 非零退出碼）
scripts/agi.sh status

# 驗收場次專屬日誌（撥打前設置，勿與生產日誌共用）
AGI_LOGS=out/calls/test-YYYYMMDD scripts/agi.sh restart
```

每次啟動／重啟後的金絲雀驗證：

```bash
tail -n 5 out/agi.log   # 必須看到 roster: source='Test Directory' people=N aliases=M
```

指令之外的語意（`scripts/agi.sh`）：

- `foreground`＝前景執行，Ctrl+C 停止、log 直接看；背景模式 PID 寫 `out/agi.pid`、log 寫 `out/agi.log`。
- 預設 listen `127.0.0.1:4573`；換埠 `AGI_LISTEN=127.0.0.1:4574 scripts/agi.sh start`。
- 換名冊（臨時驗收）：`AGI_DIRECTORY=/path/to/roster.json scripts/agi.sh start`。**systemd 不讀這個變數**——那條路的 `--directory` 寫在 unit 的 `ExecStart`，要改 unit 或加 drop-in。
- userman 模式：`AGI_PBX_DB=1 AGI_SOURCE_DIRECTORY='<你的 directory 名>' scripts/agi.sh start`
  （`AGI_ALIASES`／`AGI_DEPARTMENTS` 可換路徑）；回滾＝拿掉 `AGI_PBX_DB` 重啟。
  換其他名冊只改 `--source-directory` 一個值，補撥 2–3 通驗證即可。
- 名冊旗標（手動與 systemd 的 `ExecStart` 同一組）：
  `--pbx-db --source-directory '<你的 directory 名>' --aliases data/aliases.json --departments data/departments.json`。
- 若 `.env` 存在會自動 source，key 不進指令行、不進 shell 歷史；其他參數原樣傳給
  `agi_server.py`（例 `--health out/agi.health.json`）。
- `status` 除了 PID／port，也讀 `out/agi.health.json`（sweeper 每 1 秒寫一次）：
  heartbeat 超過 5 秒沒更新、pid 不符或檔不存在 → `sweeper: DEAD`，退出碼 1；
  `active` 是當下的 session 數，`jsonl:` 是最新一輪的 `ts`。sweeper 死掉時行程與 port
  可能都還在，但過期 session 會永佔並發閘——所以它算 liveness，不是加分項。
  `AGI_HEALTH` 可換 heartbeat 路徑（預設 `out/agi.health.json`）。
- `reload` 只支援 `start` 的背景模式（要有 `out/agi.log`）：腳本送 HUP 後等 10 秒，
  抓最新一筆 `reload ok|failed`；`reload failed` 時服務仍在跑（舊索引），退出碼 1。
  SIGHUP 只換名冊，**進行中的 session 不重置**（`TurnManager` 跨 reload 存活）。
- 背景模式看 log：`tail -f out/agi.log`。
- **不要和 systemd 同時跑**（port 衝突）。手動模式是正式模式，重開機不會自動起。

### systemd（選用，開機自啟）

```ini
# /etc/systemd/system/dial-by-name-agi.service
[Unit]
Description=dial-by-name FastAGI (Realtime STT, Test Directory roster)
After=network.target

[Service]
Type=simple
User=asterisk
Group=asterisk
WorkingDirectory=/var/lib/asterisk/dial-by-name
EnvironmentFile=/var/lib/asterisk/dial-by-name/.env
ExecStart=/var/lib/asterisk/dial-by-name/.venv/bin/python agi_server.py \
  --pbx-db --source-directory "Test Directory" \
  --logs out/calls --health out/agi.health.json
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

安裝與驗證：

```bash
sudo cp dial-by-name-agi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dial-by-name-agi
systemctl --no-pager status dial-by-name-agi   # 權威：active (running)
scripts/agi.sh status                          # 只看 sweeper 段（pid 檔不認 systemd 進程，會顯示 stopped）
grep "roster: source" out/agi.log | tail -1    # 金絲雀：people=13
```

注意事項：

- `EnvironmentFile` 只為 `ELEVENLABS_API_KEY`（`.env` 權限 600、owner asterisk）；
  名冊模式由 `ExecStart` 的 flag 決定，`AGI_*` 環境變數是 wrapper 專用。
- `ExecReload` SIGHUP＝只換名冊；**改程式碼仍需 restart**（新進程）。
- `Restart=on-failure` 是對無聲死亡的防護，不是萬靈丹——systemd 模式的權威是
  `systemctl status`，手動模式的權威是 `scripts/agi.sh status`。
- `scripts/agi.sh status` 的 pid 判定讀 `out/agi.pid`（只有 `agi.sh start` 會寫）：
  systemd 起的進程在它眼裡是 `stopped`；同一段輸出裡的 `ss` 監聽行與 sweeper heartbeat 仍有效。
- DB 連線走 `/etc/freepbx.conf` fallback；要唯讀帳號就補 `DBN_MYSQL_*` 四鍵到 `.env`。
- 本節是 systemd unit 與服務啟停的單一來源；`docs/dialplan.md` §3.5 只記部署面（權限、人員異動 SOP）並指向本章。

---

## 音檔：本 repo 不含提示音

系統會播 19 支提示音（`welcome`、`processing`、`retry`、`q-*`、`dept-*`、`transferring`…）。
**它們不在這個 repo 裡**：真人錄音／TTS 資產的再散布條件另計，請自行錄製或合成。

| 項目 | 值 |
|---|---|
| 數量與格式 | 19 支 `.wav`，**8 kHz／mono／16-bit**，檔名純 ASCII |
| 清單 | 單一真相是 `scripts/check_sounds.py` 的 `EXPECTED`（不要另外抄一份） |
| 部署位置 | **`/var/lib/asterisk/sounds/dbn/zh/`**（由 dialplan 的 `DBN_SOUND` 決定） |
| 驗證（開發端） | `uv run scripts/check_sounds.py <你的暫存目錄>`（缺檔／格式錯／非 ASCII 都會列出來） |

上傳到 PBX 的指令、目錄與權限見 `docs/dialplan.md` §1。少了 `dbn/` 這一層（只放到
`/var/lib/asterisk/sounds/zh/`）的後果是 `Playback` **全無聲、且沒有明顯報錯**——這是最常見的部署錯誤。

clip key 的三段對照：

| 決策層（`Decision.speech`） | 通道變數 | dialplan |
|---|---|---|
| `sounds/zh/q-say-full-name` | `DBN_SPEECH=q-say-full-name` | `Playback(${DBN_SOUND}/q-say-full-name)` |

決策層只吐**標籤**：`dialname/agi.py` 的 `_clip_name()` 剝掉 `sounds/zh/` 前綴，
所以 repo 內不存在 `sounds/` 目錄也不影響決策與測試。

**已知限制**：`disclose_names=True`（念出姓名澄清）需要的四支固定句
`q-is-it`／`q-or`／`q-question`／`transferring-name` **未實作**，`agi_server.py` 也沒有對應旗標。
預設 `disclose_names=False` 不會用到它們；那個分支是內線專用部署的工作，不在本 repo 範圍。

---

## 架構

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
        DBN_ACTION / DBN_REASON / DBN_TARGET / DBN_DEPT / DBN_SPEECH
             │
             ▼
        dialplan 依 DBN_* 動作：Dial(Local/…@from-internal) 或 播下一句
```

**AGI 只做 STT＋決策**；收音、播音、轉接、回合計數全在 dialplan。這個切法的好處是
**跨輪**通話狀態只有一處（dialplan）；AGI 只在一輪之內保存 session（`start` 登記、`result` 收工，
過期 session 與 wav 由 TTL 20 秒的 sweeper 清），壞掉時 dialplan 有安全網（`AGISTATUS` → 總機）。

實際部署見 `docs/dialplan.md`（手動驗收清單、人員異動 SOP）；服務啟停與（選用）systemd unit 見上面的「服務管理與開機自啟」。

### 檔案佈局

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
docs/design.md             設計與理由（不變式、決策順序、門檻、外部模型、名冊、回歸）
docs/dialplan.md           dialplan 片段、部署、手動驗收
docs/calls.md              20 通劇本協議與判定（表格由 scripts/calls.py table 產生）
docs/glossary.md           欄位／列舉值字典、常數表
docs/io-mapping.md         五段 I/O 邊界、Decision → PBX
docs/provenance.md         這個公開版的來源
data/people.json           40 人／8 部門的合成示範名冊
data/departments.json      8 部門定義（dept_id 契約）
data/call_script.json      20 通劇本的機器判定
scripts/                   agi.sh／calls.py／check_sounds.py／check_aliases.py／jev_probe.py／install-hooks.sh
tests/                     241 個 pytest（L0 golden、L1 劇本、AGI、Realtime、名冊、alias 政策…）
```

---

## 7 條不變式

改任何東西之前先確認這七條還成立（完整理由與決策順序見 `docs/design.md` §1）：

1. **決策順序不可調換**。
2. **同音兄弟一律本地解**：跨部門問部門、同部門 fallback，不猜。
3. **「不需澄清」與「只有一個候選」優先於任何追問**。
4. **(d) 只問一次**：第二輪仍拉鋸 → `clarify-exhausted`；任何路徑不得產生第三次追問。
5. **決策層零外部呼叫**：`decide()` 沒有委派參數，`dialname/` 內沒有通往外部 API 的路徑。
6. **部門路由鍵是 ASCII `dept_id`**；中文部門名只進 log，不進檔名／dialplan／通道變數。
7. **入口只 import `dialname`**：`eval.py`／`trace.py`／`demo.py`／`agi_server.py`／`stt_batch.py`
   不得自行實作正規化、評分、門檻或決策。

---

## 為什麼不讓模型直接吃整本電話簿

500 個人的候選清單同時造成三件事：token 成本與延遲隨人數線性上升；選項一多，
機率分布被攤平、`confidence` 失去鑑別度；同音字（王/汪、張/章）本來就只有**發音**能分辨，
把判斷丟給語言模型等於要它做字串比對——那是本機 3 ms 就能做完的事。

所以分工是「本地索引收斂候選 → 本地決策」，**整本電話簿永遠不出機器**。
現行版本連「少數候選」也不外送：2026-11-21 起決策層零外部模型存取，
理由、可達率證據與重開條件見 `docs/design.md` §4。

---

## 評分與決策（摘要）

```
score = 0.20 × 漢字逐字 + 0.16 × 漢字部分 + 0.30 × 姓拼音
      + 0.14 × 字數 + 0.10 × 名拼音 + 0.10 × 聲母序
部門：同部門 +0.10、不同部門 −0.08；職稱 +0.04
```

優先序是**姓 > 字數 > 名**：中文姓是封閉集合（幾百個），同音的姓幾乎等於同一個人；
名字發散，同音只能當輔助。門檻與權重的數值、以及「為什麼是這些值」見 `docs/design.md` §3。

決策十條順序（掛線／部門／重問／只講姓／漏字／不需澄清／單一候選／同音兄弟／問一句）
見 `docs/design.md` §2；那是**程式碼順序**，調換就是改行為。

---

## 同音：三種情況

| 情況 | 例 | 本地能做的 | 剩下交給誰 |
|---|---|---|---|
| A. 真同音，只有一個人 | 名冊只有張家豪，STT 給章家豪 | 拼音相同 → 1.0 命中 | 直接轉 |
| B. 真同音，兩個都在 | 張家豪（工程）、章家豪（財務） | 兩個都高分、差距小 | 一定先澄清 |
| C. 字面命中另一個人 | 查「汪偉」，名冊有王偉（業務）與汪偉（客服） | top1 1.00、margin 0.21，看起來很有信心 | ⚠️ 最危險 |

C 是純 margin 檢查抓不到的：分數差看起來安全，但兩個人**同音**，而 STT 的錯誤正是
把「王」聽成「汪」。所以除了第二名的分數門檻，還有 `same_surname_homophone()`
（同姓＋名同音不同字，不看分數）與 `confusable()`（tight／loose 拼音鍵交集）。

---

## 隱私

電話簿是「撥得到就能查」的服務。系統只要開口念出名字，就等於提供列舉介面
（換 20 個姓氏就能蒐集全公司名單）。三條規則（`disclose_names=False`，預設）：

| 規則 | 做法 |
|---|---|
| 不念名字 | 澄清一律播「請說全名」；轉接只播「為您轉接」 |
| 不確認存在 | 回覆內容與「這個人存不存在」無關，避免變成 oracle |
| 不列舉數量 | 不說「有 2 位符合」 |

**按鍵只用來選「部門／總機」，永遠不用來盲選人。** 沒有名字的「按 1／按 2」對來電者
毫無意義：他如果分得出是哪一位就不需要澄清；分不出來，更不可能知道 1 和 2 各是誰。

唯一的第三方是 STT（ElevenLabs Scribe v2），它聽到的本來就是來電者的語音。
`data/aliases.json`（小名 sidecar）含真名，不入版控；`data/aliases.example.json` 是範本。

---

## 量測

### 合成測資（回歸訊號，不是準確率）

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

消融（同音＋報部門的 top-1）：部門加權與部門收斂單獨開啟都能到 1.000，
兩者都關掉是 0.444；現況兩者都開。分工與門檻理由見 `docs/design.md` §3.2。

### 效能（純 Python，單核）

| 名冊人數 | 首音節桶＋低信心回掃 | 暴力全掃 | 加速 |
|---|---|---|---|
| 40 | 1.96 ms | 5.24 ms | 2.7× |
| 200 | 2.00 ms | 23.95 ms | 12.0× |
| 500 | 4.84 ms | 63.99 ms | 13.2× |
| 2000 | 12.09 ms | 206.99 ms | 17.1× |

兩個關鍵優化：查詢的拼音特徵只算一次；首音節桶＋低信心回退全掃（同音字的拼音首音節
必然相同，所以桶不會漏掉同音候選）。

### 真實通話

`eval.py --calls` 可以吃「通話日誌＋truth.csv」算同一套指標：通話中 AGI 只寫 JSONL
（逐字稿＋決策），撥完之後才把正確答案接上去評分，答案卡不參與通話當下的任何決策。
欄位與流程見 `docs/calls.md` §4 與 `docs/dialplan.md`。

**本 repo 不含任何真實通話語料**（含個資）。真實 WER 請用自己的錄音量。

---

## 已知限制

- **真實 WER 未量測**：8 kHz 窄頻＋台灣國語＋姓名，STT 的錯誤型態是可觀察的「整詞替換」，
  但比例要靠你自己的錄音。合成測資不回答這個問題。
- **keyterms 未啟用**：ElevenLabs 的 keyterms 可用來 bias 辨識，但模型輸出簡體、
  送繁體 keyterm 可能白費；需要 A/B 驗證（`stt_batch.py --audio-dir … --truth …` 三組共用同一份 truth）。
- **`disclose_names=True` 未完整**：見上面的音檔章節。
- **STT 全斷沒有 fast-fail**：WS 連不上／中斷且無 transcript → `fallback`／`stream-error` → 總機；
  `result` 端總預算 3 秒（`RESULT_TIMEOUT`），沒有熔斷、沒有指數退避。401／403 與壞 key
  一律落總機（2026-11-23 翻案 PoC 的掛線；`auth`／`ws` 分界見 `docs/io-mapping.md` §3）。
- **同部門同音兄弟**：示範名冊的同音對都刻意跨部門，沒有同部門同音對可測；真機第二場
  遇到同部門同音時行為＝`homophone-unresolvable` → 總機（安全拒猜），合成測資未覆蓋。
- **Go 移植**只有對照表（`docs/design.md` §3 的常數是唯一來源），沒有實作。

---

## 回歸與變更紀律

- 改 `dialname/`、`eval.py`、`data/people.json` 之後跑 `uv run pytest -q`；`scripts/install-hooks.sh`
  裝的 pre-commit 會在這些路徑 staged 時先跑 L0／L1，非零即拒絕 commit。
- **不要為了讓測試過而放寬門檻**：先判斷是規則錯還是測資錯。
- 刻意改行為 → 用 `scripts/calls.py update` 搬 `expect` 並看 diff，再依 `tests/golden/README.md`
  更新 golden，並在 `CHANGELOG.md` 記一筆。
- 版本變更見 `CHANGELOG.md`；下一步見 `ROADMAP.md`；安全回報見 `SECURITY.md`。
