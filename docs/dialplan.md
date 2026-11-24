# Dialplan：AGI 只做 STT＋決策，其餘交給 FreePBX

> 這份片段由使用者**手動**貼進 `/etc/asterisk/extensions_custom.conf`，agent 不遠端改 PBX。
> 規則：`;` 之後可以是中文註解，**其餘一律 ASCII**（`tests/test_dialplan_ascii.py` 會驗）。

## 1. 套用前準備

| 項目     | 指令／說明                                                                                                            |
| -------- | --------------------------------------------------------------------------------------------------------------------- |
| 音檔     | 本 repo **不含音檔**：自備 19 支提示音（8 kHz／mono／16-bit、檔名 ASCII），先 `uv run scripts/check_sounds.py <暫存目錄>` 驗證，再 `rsync -av <暫存目錄>/ pbx:/var/lib/asterisk/sounds/dbn/zh/`——**`dbn/` 這一層不能省**，它必須與 §2 的 `DBN_SOUND` 逐字一致（少一層＝`Playback` 全無聲且無明顯報錯） |
| `.env`   | 專案根目錄；`ELEVENLABS_API_KEY`（＋ userman 模式的 `DBN_MYSQL_*`）；`chmod 600`、不進版控（見 §1.1）                                |
| 錄音目錄 | `mkdir -p /home/pbx/dial-by-name/recordings && chown asterisk:asterisk … && chmod 0770 …`                             |
| 日誌目錄 | `mkdir -p /home/pbx/dial-by-name/out/calls && chown <agi-user>:asterisk …`                                            |
| 權限驗證 | `sudo -u asterisk touch recordings/.probe && rm recordings/.probe`；`out/calls` 同樣做一次                            |
| 總機落點 | 確認 FreePBX `General Settings → Operator Extension`，或直接把 `DBN_OPERATOR` 改成總機分機                            |
| 目的地   | 一律 `Dial(Local/<num>@from-internal)`；號碼背後是分機、queue 或 ring group 都一樣，只要號碼在 PBX 上有效就會通       |
| 小名 sidecar | userman 模式才需要：`data/aliases.json`（稀疏、含真名，**不入版控**；範本 `data/aliases.example.json`），權限 600；**rsync 加 `--exclude data/aliases.json`** 防更新覆蓋；清 log 時排除此檔   |
| DB 帳號  | userman 模式：唯讀帳號（`SELECT ON asterisk.*`）＋ `.env` 四變數；fallback `/etc/freepbx.conf` 需服務帳號可讀          |
| 部門定義 | `data/departments.json`（入版控）；改 `dept_id` 要連 queue 對映與 `dept-<id>` 音檔一起動                              |

> `recordings/` 與 `out/calls/` **不進版控**：實撥完把 PBX 上這兩個目錄複製回本機同名位置
> （傳輸方式自選），`eval.py --calls` 才有資料可比對。

### 1.1 `.env`（API key 與 DB 連線）

放在**專案根目錄**（與 `agi_server.py` 同層）：

```
/home/pbx/dial-by-name/.env
```

內容**只放 `KEY=value`**，**不要加 `export`**（systemd 的 `EnvironmentFile` 只認這種格式）：

```dotenv
ELEVENLABS_API_KEY=
DBN_MYSQL_HOST=127.0.0.1
DBN_MYSQL_USER=dbn_ro
DBN_MYSQL_PASS=
DBN_MYSQL_DB=asterisk
```

| 變數 | 用途 | 何時需要 |
|---|---|---|
| `ELEVENLABS_API_KEY` | ElevenLabs Scribe v2 STT | 每次 STT |
| `DBN_MYSQL_HOST` / `_USER` / `_PASS` / `_DB` | userman 唯讀連線（四鍵**全有**才用 `.env`） | `--pbx-db` |

- 四鍵**整組二擇一，不混搭**：任一缺 → fallback 解析 `/etc/freepbx.conf` 的
  `$amp_conf['AMPDBHOST'/'AMPDBUSER'/'AMPDBPASS'/'AMPDBNAME']`（只認賦值行）；
  兩邊都不齊或連線失敗 → **拒絕啟動**（不以空名冊硬跑）。
  freepbx.conf 的 `AMPDBUSER` 常是 root——那是應急門，正式環境用 `.env` ＋唯讀帳號。

- `chmod 600 .env`；systemd 用 `User=asterisk` 時 `chown asterisk:asterisk .env`。
- 不進版控、不進 rsync 樹；`scripts/agi.sh` 手動啟動時會自動 source。
- 其他非敏感變數：`AGI_LISTEN`（預設 `127.0.0.1:4573`）、`AGI_LOGS`（預設 `out/calls`）、
  `AGI_DIRECTORY`（預設 `data/people.json`）、`AGI_PBX_DB=1`＋`AGI_SOURCE_DIRECTORY`
  （userman 模式）、`AGI_ALIASES`、`AGI_DEPARTMENTS`——只給 `scripts/agi.sh` 用，systemd 不讀。

### 1.2 程式與執行環境（PBX 上）

PBX 不共用本機的 `.venv`（Python 版本與平台都不同），要用 `uv` 重建。

```bash
# 0) uv 必須存在（沒有就先裝：https://docs.astral.sh/uv/）
command -v uv || echo "uv MISSING"

# 1) 取得程式到 ~/dial-by-name（git clone 或你慣用的複製方式）
#    不要帶 .venv／out／.env／recordings：那是本機產物與金鑰
cd ~/dial-by-name
#
# 之後每次從開發機更新的標準指令（--chown：rsync -a 會鏡像開發機的 uid/gid，
# 加上它才會一律落地 asterisk:asterisk；寫入面 out/／recordings/／.env／
# data/aliases.json 全在排除清單，同步進去的只有唯讀程式碼。rsync 兩端需 >=3.1）：
#
#   rsync -av --chown=asterisk:asterisk \
#     --exclude .venv --exclude out --exclude .env \
#     --exclude recordings --exclude 'data/aliases.json' \
#     --exclude .git --exclude __pycache__ \
#     ./ <pbx>:/var/lib/asterisk/dial-by-name/

# 2) 建立虛擬環境（依 pyproject.toml + uv.lock）
uv sync

# 3) 啟動腳本必須可執行（從壓縮檔解出來的會掉 mode）
chmod +x scripts/agi.sh

# 4) key 是否載入（只印有沒有，不印值）
set -a && . ./.env && set +a
for k in ELEVENLABS_API_KEY; do
  [ -n "${!k}" ] && echo "$k set" || echo "$k MISSING"
done
```

驗證：

- `uv run python -c "import dialname.core"` 沒有錯誤。
- 步驟 4 兩行都是 `set`（`MISSING` 就回去看 §1.1 的路徑與權限）。
- `ls .venv/bin/python` 存在；之後 systemd／`scripts/agi.sh` 都用這個直譯器。

## 2. 片段

```
[dbn-main]
exten => s,1,Answer()
 same => n,Set(DBN_ATTEMPT=1)
 same => n,Set(DBN_SEQ=1)
 same => n,Set(DBN_TRIES=0)
 same => n,Set(DBN_CANDS=)
 same => n,Set(DBN_REC_OPTS=o)
 same => n,Set(DBN_SOUND=/var/lib/asterisk/sounds/dbn/zh)
 ; DBN_OPERATOR 與下面 dept_* 的 queue 號碼都是範例，照你的 PBX 換
 same => n,Set(DBN_OPERATOR=1000)
 same => n,Set(DBN_SPEECH=)
 same => n,Playback(${DBN_SOUND}/welcome)
 same => n(rec),Set(DBN_SPEECH=)
 ; 每輪重置：AGI 若靜默失敗（一個變數都沒送），落在 fallback → 總機，不沿用上一輪的動作
 same => n,Set(DBN_ACTION=fallback)
 same => n,Set(DBN_WAV=/var/lib/asterisk/dial-by-name/recordings/${UNIQUEID}-${DBN_SEQ})
 ; silence=4s、max=10s、DBN_REC_OPTS=o 是按 0 轉總機（第二輪會清空）。不加 q 才會播 beep。
 same => n,Record(${DBN_WAV}.wav,4,10,${DBN_REC_OPTS})
 same => n,GotoIf($["${RECORD_STATUS}"="OPERATOR"]?operator)
 same => n,GotoIf($["${RECORD_STATUS}"="HANGUP"]?done)
 same => n,GotoIf($["${RECORD_STATUS}"="ERROR"]?retry)
 same => n,Playback(${DBN_SOUND}/processing)
 same => n,AGI(agi://127.0.0.1:4573,${DBN_WAV}.wav,${DBN_ATTEMPT},${DBN_CANDS})
 same => n,GotoIf($["${AGISTATUS}"!="SUCCESS"]?operator)
 same => n,GotoIf($["${DBN_ACTION}"="route"]?route)
 same => n,GotoIf($["${DBN_ACTION}"="route_dept"]?dept_dispatch)
 same => n,GotoIf($["${DBN_ACTION}"="ask"]?ask)
 same => n,GotoIf($["${DBN_ACTION}"="retry"]?retry)
 same => n,GotoIf($["${DBN_ACTION}"="hangup"]?hangup)
 same => n,Goto(fallback)

 same => n(hangup),GotoIf($["${DBN_SPEECH}"!=""]?hangup_play)
 same => n,Set(DBN_SPEECH=goodbye)
 same => n(hangup_play),Gosub(dbn-play-speech,s,1)
 same => n,Hangup()

 ; fallback：先播 decide() 給的說明（not-found），再轉總機。
 ; 只有這一條會播 DBN_SPEECH；Dial 失敗／AGISTATUS／按 0 只播 to-operator，
 ; 否則會把剛才播過的 transferring 再播一次。
 same => n(fallback),GotoIf($["${DBN_SPEECH}"!=""]?fallback_play)
 same => n,Goto(operator)
 same => n(fallback_play),Gosub(dbn-play-speech,s,1)
 same => n,Goto(operator)

 same => n(ask),GotoIf($["${DBN_ATTEMPT}" >= "2"]?operator)
 same => n,Set(DBN_ATTEMPT=$[${DBN_ATTEMPT}+1])
 same => n,Gosub(dbn-play-speech,s,1)
 same => n,Set(DBN_SEQ=$[${DBN_SEQ}+1])
 ; 追問部門的那一輪不再提供按 0 逃生口（只能回答部門；兩次沉默仍會掛線）
 ; 注意：「沒聽清楚」的重試走 (retry)，不經過這裡，仍保留按 0。
 same => n,Set(DBN_REC_OPTS=)
 same => n,GotoIf($["${DBN_ATTEMPT}" >= "2"]?ask_rec)
 ; Background 才會收 DTMF；按 0 立刻轉總機，不必等錄音開始
 same => n,Background(${DBN_SOUND}/q-operator)
 same => n(ask_rec),Goto(rec)

 same => n(retry),Set(DBN_TRIES=$[${DBN_TRIES}+1])
 same => n,GotoIf($["${DBN_TRIES}" > "1"]?hangup)
 same => n,Playback(${DBN_SOUND}/retry)
 same => n,Set(DBN_SEQ=$[${DBN_SEQ}+1])
 same => n,Goto(rec)

 same => n(route),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/${DBN_TARGET}@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_dispatch),GotoIf($["${DBN_DEPT}"="sales"]?dept_sales)
 same => n,GotoIf($["${DBN_DEPT}"="cs"]?dept_cs)
 same => n,GotoIf($["${DBN_DEPT}"="rd"]?dept_rd)
 same => n,GotoIf($["${DBN_DEPT}"="fin"]?dept_fin)
 same => n,GotoIf($["${DBN_DEPT}"="wh"]?dept_wh)
 same => n,GotoIf($["${DBN_DEPT}"="adm"]?dept_adm)
 same => n,GotoIf($["${DBN_DEPT}"="hr"]?dept_hr)
 same => n,GotoIf($["${DBN_DEPT}"="it"]?dept_it)
 same => n,Goto(operator)

 same => n(dept_sales),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/2100@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_cs),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/2200@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_rd),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/3100@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_fin),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/4100@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_wh),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/5100@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_adm),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/6100@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_hr),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/6200@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(dept_it),Gosub(dbn-play-speech,s,1)
 same => n,Dial(Local/7100@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Goto(operator)

 same => n(operator),Playback(${DBN_SOUND}/to-operator)
 same => n,Dial(Local/${DBN_OPERATOR}@from-internal,30)
 same => n,GotoIf($["${DIALSTATUS}"="ANSWER"]?done)
 same => n,Playback(${DBN_SOUND}/goodbye)
 same => n,Goto(done)
 same => n(done),Hangup()

 ; 只給澄清句的 Background 用：0 轉總機，其他按鍵忽略並繼續收音
 exten => 0,1,Goto(s,operator)
 exten => _X,1,Goto(s,rec)
 exten => i,1,Goto(s,rec)

[dbn-play-speech]
exten => s,1,Set(DBN_IDX=1)
 same => n(split),Set(DBN_CLIP=${CUT(DBN_SPEECH,&,${DBN_IDX})})
 same => n,GotoIf($["${DBN_CLIP}"=""]?done)
 same => n,Playback(${DBN_SOUND}/${DBN_CLIP})
 same => n,Set(DBN_IDX=$[${DBN_IDX}+1])
 same => n,Goto(split)
 same => n(done),Return()
```

## 3. 套用（GUI 接入）

> **順序**：先照 §3.5／§3.6 啟動 FastAGI、跑完 §3.8 冒煙，再貼 dialplan（§3.1）。
> AGI 沒起來時撥號會走 `AGISTATUS` 安全網轉總機，很容易誤判成 dialplan 寫錯。

### 3.1 貼上 dialplan

Admin → **Config Edit** → `extensions_custom.conf`，貼上 `[dbn-main]` 與
`[dbn-play-speech]` 兩個 context。

> 不要放進 `[from-internal-custom]`：它永遠被 include 進 `from-internal`，裡面的
> `exten => s` 會變成內部分機可撥，也可能蓋到既有 dialplan。獨立 context 才是可預期的。

### 3.2 建立 Custom Destination

Admin → **Custom Destinations** → Add：

| 欄位 | 值 |
|---|---|
| Description | `Dial by Name` |
| Target | `dbn-main,s,1` |
| Return | **Off**（本流程以 `Dial`／`Hangup` 結束，不需要回上層） |

### 3.3 接到電話（Inbound Route）

Connectivity → **Inbound Routes** → 選 `Custom Destinations → Dial by Name`。
IVR／Time Condition 等其他模組也用同一個 destination。

### 3.4 內部分機測試（可選）

不要改 `from-internal-custom`；用 Admin → **Misc Applications**（或 Misc Destination）：

| 欄位 | 值 |
|---|---|
| Feature Code | `*8000`（或你指定的號碼） |
| Destination | `Custom Destinations → Dial by Name` |

這樣內部分機撥 `*8000` 就會進 `[dbn-main]`，全程 GUI 管理。

### 3.5 FastAGI 服務（systemd）

`agi_server.py` 必須常駐；`WorkingDirectory` 一定要設，否則相對路徑（`data/people.json`、
`out/calls`）會依 systemd 預設 cwd 出錯。

```ini
[Unit]
Description=dial-by-name FastAGI
After=network.target

[Service]
User=asterisk
Group=asterisk
WorkingDirectory=/home/pbx/dial-by-name
EnvironmentFile=/home/pbx/dial-by-name/.env
ExecStart=/home/pbx/dial-by-name/.venv/bin/python agi_server.py \
  --listen 127.0.0.1:4573 --directory data/people.json --logs out/calls
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

userman 模式（FreePBX 的 User Management → Directories 裡你自己那個 directory）：把 `ExecStart` 的名冊參數換成

```
  --pbx-db --source-directory '<你的 directory 名>' \
  --aliases data/aliases.json --departments data/departments.json
```

（最終上線若換其他名冊：只改 `--source-directory` 一個值，補撥 2–3 通驗證即可。）

`--pbx-db` 的 DB 設定讀 `.env` 四變數（缺任一才 fallback `/etc/freepbx.conf`）；
兩邊都不齊或連線失敗＝啟動失敗。source directory 打錯／0 人同樣拒絕啟動——
不要讓「空名冊成功啟動」把每一通電話靜默轉總機。

```bash
sudo $EDITOR /etc/systemd/system/dial-by-name-agi.service   # 貼上上面的 unit
sudo systemctl daemon-reload
sudo systemctl enable --now dial-by-name-agi
ss -ltnp | grep 4573
journalctl -u dial-by-name-agi -f
```

**人員異動 SOP（userman 模式）**

1. userman 改人／外部目錄同步完 → **不需要** `sudo fwconsole reload`（Apply Config）：
   userman 的 `genConfig()`／`writeConfig()` 是空實作，姓名、部門、分機綁定只寫
   `userman_users`，不產生任何 Asterisk 設定；AGI 直接 SELECT 該表。
   外部目錄（AD/LDAP）先 `sudo fwconsole userman --syncall --force` 拉進 DB。
   **例外**：同一批操作若新增／改動分機（Extensions，含 Quick Create 建的使用者）、
   queue、ring group、inbound route，或改了 `extensions_custom.conf`（含 `dept-*` 對映），
   才需要 `sudo fwconsole reload`——那是 Asterisk 設定，漏了它
   `Dial(Local/<ext>@from-internal)` 會找不到新分機。
2. 離職：同步刪除 `aliases.json` 裡該 `user_id`（孤兒 alias 會讓 reload 永遠失敗）。
3. `uv run scripts/check_aliases.py --source-directory '<你的 directory 名>'` → 退出碼 0。
4. `sudo systemctl reload dial-by-name-agi`（PoC 手動模式：`scripts/agi.sh reload`）→ 必須看到
   `reload ok: people=N aliases=M (was K)`；`reload failed` 時**服務仍用舊索引**，修好再 reload。
5. `aliases.json`／`departments.json` 一律「寫暫存檔 → `mv` 取代」，避免半寫入檔案碰上 HUP。

**權限（二選一，必須一致）**

- 方案 A（本範例）：`User=asterisk`；`.env` 必須是 `asterisk:asterisk 600`，
  專案目錄可被 asterisk 讀取，`recordings/` 與 `out/calls/` 都是 `asterisk:asterisk 0770`：
  `sudo chown asterisk:asterisk .env && sudo chmod 600 .env`
- 方案 B：`User=pbx`、`Group=asterisk`、`SupplementaryGroups=asterisk`；`.env` 是 `pbx:pbx 600`；
  `recordings/`／`out/calls/` 給 `asterisk:asterisk 0770`（pbx 靠群組讀寫）。

### 3.6 手動啟動（PoC）

不需要 systemd；適合單機 PoC／除錯。**不要和 systemd 同時跑**（port 衝突）。

```bash
cd /home/pbx/dial-by-name
scripts/agi.sh foreground        # 前景執行，Ctrl+C 停止，log 直接看
scripts/agi.sh start             # 背景啟動；PID 寫 out/agi.pid、log 寫 out/agi.log
scripts/agi.sh status            # 看 PID 與 4573 是否在聽
scripts/agi.sh reload            # SIGHUP → 等 log 的 reload ok/failed；失敗回非零
scripts/agi.sh stop
scripts/agi.sh restart
```

- 預設 listen `127.0.0.1:4573`；換埠：`AGI_LISTEN=127.0.0.1:4574 scripts/agi.sh start`。
- 換名冊（臨時驗收用）：`AGI_DIRECTORY=/path/to/roster.json scripts/agi.sh start`。
  **systemd 不讀這個變數**——那條路的 `--directory` 寫在 unit 的 `ExecStart`，要改 unit 或加 drop-in。
- userman 模式：`AGI_PBX_DB=1 AGI_SOURCE_DIRECTORY='<你的 directory 名>' scripts/agi.sh start`
  （`AGI_ALIASES`／`AGI_DEPARTMENTS` 可換路徑）；回滾＝拿掉 `AGI_PBX_DB` 重啟。
- 若 `.env` 存在會自動 source，key 不進指令行、不進 shell 歷史。
- 其他參數原樣傳給 `agi_server.py`，例：`scripts/agi.sh start --stt-timeout 15`。
- `reload` 只支援 `start` 的背景模式（要有 `out/agi.log`）：腳本送 HUP 後等 10 秒，
  抓最新一筆 `reload ok|failed`；`reload failed` 時服務仍在跑（舊索引），退出碼 1。
- 背景模式看 log：`tail -f out/agi.log`。
- 手動模式**重開機不會自動起**；正式部署請用上面的 systemd。

> `.env` 權限 600、不入版控、不進 rsync 樹；User/Group 要與 `recordings/`、`out/calls/`
> 的權限一致。handler thread 超時後只關閉連線，**不保證停止仍在跑的 STT 執行緒**
> （它會在自己的 timeout 內結束）。

### 3.7 套用後驗證

```bash
sudo fwconsole reload
sudo asterisk -rx 'dialplan show dbn-main'
sudo asterisk -rx 'dialplan show dbn-play-speech'
sudo asterisk -rx 'dialplan show dbn-main@dbn-main'   # 應看到 s,1 Answer()
```

### 3.8 第一通之前的 AGI 冒煙（不必撥號）

**先確認服務活著**：

```bash
cd ~/dial-by-name
scripts/agi.sh start          # 或 sudo systemctl start dial-by-name-agi
scripts/agi.sh status         # PID 存在、4573 在聽
ss -ltnp | grep 4573
tail -n 5 out/agi.log         # 應有 "FastAGI listening on 127.0.0.1:4573"
```

**STT 對外通不通**（key 有效、外網可達、模型可呼叫）：

```bash
uv run stt_batch.py --audio sounds/zh/welcome.wav
```

**決策管線通不通**（不經電話、不經協議層，直接呼叫 handler）：

```bash
uv run python -c "
from dialname.core import load_directory
from dialname.stt_client import SttClient
from dialname.agi import handle_turn
d = load_directory('data/people.json')
print(handle_turn('sounds/zh/welcome.wav', 1, stt=SttClient(timeout=12.0), directory=d))
"
```

判讀：`welcome` 的內容是「請說出您要找的部門或姓名」，含路由意圖詞「找」，
所以**預期得到 `action=ask`／`reason=no-candidate-retry`**（有找人意圖但沒有候選）。
出現 `retry` 代表 STT 回空字串或呼叫失敗（音檔、key、網路）；`hangup`／`no-intent`
代表意圖詞判定異常；`route`／`route_dept` 代表名冊或比對異常。
三項都對再進 §4 撥號。

### 3.9 STT 失效時會怎樣

`handle_turn()` 只判斷「要不要重試」；**決定何時掛線的只有 dialplan 的 `DBN_TRIES`**。
adapter 拿不到通話層狀態，不得自行判斷「第二次空字串就掛線」。

| 情況 | `DBN_ACTION` | `DBN_REASON` | 來電者聽到 | 誰決定 |
|---|---|---|---|---|
| 逐字稿空字串 | `retry` | `stt-empty` | `retry`（沒聽清楚，請再說一次） | AGI |
| timeout／連線失敗／5xx／其他 4xx | `retry` | `stt-error` | 同上 | AGI |
| 401／403 | `hangup` | `stt-auth` | `goodbye` → 掛斷 | AGI（不重試、不轉總機） |
| 未預期例外（程式錯誤） | `fallback` | `internal-error` | `to-operator` → 總機 | AGI |
| 回傳值含非 ASCII 或控制字元 | `fallback` | `non-ascii` | 同上 | AGI |
| `retry` 第二次（`DBN_TRIES` > 1） | `hangup` | — | `goodbye` → 掛斷 | **dialplan** |
| AGI 沒回應／超過 25 秒 | （無） | — | `to-operator` → 總機 | dialplan（`AGISTATUS`） |

一次「STT 全掛」（不是 401，而是 timeout）的實際順序：

```
welcome → Record(上限 10s、靜音 4s) → processing → AGI
   └ 12 秒 timeout → retry → 播「沒聽清楚，請再說一次」
Record 再一次 → processing → AGI
   └ 12 秒 timeout → DBN_TRIES=2 → goodbye → Hangup
```

**至少 24 秒是純等待**（兩次 12 秒 timeout），加上兩次錄音與三句提示音，最壞約 50 秒才掛線。
不會靜默斷線，但這段時間來電者大部分在聽空白（限制見 §5）。

三條語意（改動前先讀）：

1. `retry` **不增加** `DBN_ATTEMPT`：它是同一輪重錄，不是追問；按 0 的逃生口也還在。
2. `DBN_TRIES` **整通電話共用、上限 1**：第一次空用掉重試後，之後任何一次空（不論 `attempt`）都掛線。
   `RECORD_STATUS=ERROR`（錄音本身失敗）走同一條路，**吃掉同一份預算**——
   所以「一次錄音失敗＋一次 STT 失敗」就會掛線；除錯時先看 JSONL 的 `wav_duration_ms` 是不是 `null`。
3. 401／403 刻意**不轉總機**：key 壞掉時每一通都灌總機會壓垮總機。代價是來電者被掛線，
   而且目前只有 `out/calls/*.jsonl` 的 `stt_status` 看得到（見 §5）。
   這是 **PoC 的立場**：2026-11-16 已改判為「系統故障必須落到人」，正式階段隨 Realtime 重做，
   不要沿用本條的理由。

## 4. 驗收紀錄與待驗項目

**首次上線驗收（2026-11-16）**：16 項全部通過。
環境：FreePBX **17.0.33.2**、Asterisk **22.4.1**、名冊 40 人、alias 0 筆。

**alias 政策驗收（2026-11-18）**：6 項通過（含兩條 11-17 回歸）。
環境同上；名冊 40 人＋3 個 alias（阿明／小美／筱美，內嵌在 `data/people.json`）。

`[x]` 代表**當次通過**，不是「以後不必再驗」。每次重新部署、改 dialplan 或改比對／決策邏輯後，
要重跑一輪；任一項失敗就回滾到上一個版本。

### 4.1 首次上線（已完成，2026-11-16）

- [x] §1.2 環境檢查通過（`uv sync`、`chmod +x scripts/agi.sh`、兩個 key 都印 `set`）
- [x] §3.8 冒煙通過（`welcome` 音檔 → `ask`／`no-candidate-retry`；`retry` 代表 STT 有問題）
- [x] 從 Inbound Route（或內部分機 `*8000`）進線，聽到 `welcome`（`Playback` 無聲＝路徑錯；看 `PLAYBACKSTATUS`）
- [x] 說完一句後 `recordings/<uniqueid>-1.wav` 存在，`out/calls/YYYYMMDD.jsonl` 有紀錄
- [x] `DBN_ACTION=route` 時轉到正確分機；`Dial` 失敗會轉總機（**對 queue 目的地不會觸發**：queue 立刻應答，`DIALSTATUS=ANSWER`）
- [x] 只說「業務部」→ `route_dept`、播 `transferring&dept-sales`
- [x] 完全不出聲兩次 → 播 `goodbye` 後掛線（不是轉總機）
- [x] 說「我要談業務合作」→ ~~第一輪播 `goodbye` 後掛線（不問第二輪）~~（11-16 舊規則的行為）
      **方案 C（2026-11-19）後改為 `dept-scoped-ask`**：播 `dept-sales`＋`q-dept-who` 再問一輪；
      2026-11-23 實撥重驗（T1 `dept-scoped-ask`、第二輪仍無人 → giveup 轉總機）
- [x] 說「我要找陳美玲」→ 追問部門 → 答「客服部」轉 10XX；答錯部門（如「人事部」）播 `not-found` 後轉總機，不得轉人事部
- [x] 按 0 在錄音期與澄清句（`Background`）期間都有效；`welcome`／`processing`（`Playback`）期間無效
- [x] 第一輪錄音按 0 → 總機；**「追問部門」的那一輪**按 0 無效（`DBN_REC_OPTS` 已清空、也不播 `q-operator`）
- [x] 「沒聽清楚」的重試錄音**仍可按 0**（`retry` 不增加 `DBN_ATTEMPT`，屬於同一輪；那是說不出話的來電者唯一的逃生口）
- [x] 錄音前聽得到 beep（找不到 `/var/lib/asterisk/sounds/en/beep.*` 就得改回自錄 cue）
- [x] `sudo -u asterisk touch recordings/.probe` 與 `out/calls` 的 `.probe` 都成功
- [x] `DBN_OPERATOR` 的落點正確（FreePBX Operator Extension 或實際總機分機）
- [x] AGI 掛掉時（暫時停掉 `agi_server`）撥號會轉總機，不會靜默斷線

### 4.2 alias 政策（已完成，2026-11-18）

alias 白名單政策於 2026-11-17 上線。`data/people.json` 內嵌 3 個示範 alias
（阿明／小美／筱美），所以下表三個情境直接跑預設名冊就驗得到——**不需要換名冊**。
（正式 userman 模式沒有 alias 欄位，已改由 sidecar `aliases.json` 提供，key＝`user_id`；
JSON 示範名冊仍內嵌 alias 供離線開發與本節驗收。）

2026-11-17 首輪在「小美」題抓到兩個 bug（贅詞讓同音 alias 消失、0.51 的唯一候選直轉），
修正後 2026-11-18 重撥，下表三個情境全部走過。證據：`out/calls/20261118.jsonl`（19 行／14 通）。

| 情境 | 念 | 預期 |
|---|---|---|
| 唯一命中 | 阿明 | `route` **10XX**（林志明），`reason=direct:confident` |
| 同音多人 | 小美 | `ask`（`alias-ask`）→ 答「客服部」→ `route` **10XX**；答「行政部」→ **10XX** |
| 未登記 | 美玲 | `fallback`（`alias-not-found`）→ **總機**（不掛線、不得轉給林志明） |

> 本節與後面的驗收紀錄，分機一律寫成 `10XX`（範例）。示範名冊（`data/people.json`）
> 給的是具體值；換成你的名冊時，只要「決策正確、分機指向該人」就算通過。

```bash
# 撥上面三通，看逐輪日誌（candidates 應是 0.95 的 alias 分數）
tail -n 6 out/calls/$(date +%Y%m%d).jsonl

# 需要換另一份名冊驗收時（可選）：
#   AGI_DIRECTORY=/path/to/roster.json scripts/agi.sh start
#   systemd 不讀這個變數，要改 unit 的 --directory；測完記得重啟回預設。
```

- [x] 阿明 → 直轉 10XX（`1794988132.238`，u006 0.95，`direct:confident`）。JSONL 的 candidates
      只有 `pid`／`score`，沒有 `reasons` 欄位——驗證方式就是對這兩個值
- [x] 小美 → 問部門（`1794988224.246` 第 1 輪 `alias-ask`，u008／u009 同分 0.95）；
      答客服部 → 10XX（同通第 3 輪）；答行政部 → 10XX（`1794990221.279` 第 2 輪，u009 0.95）；
      答錯部門（資訊部）→ `no-candidate-giveup` → 總機（`1794988299.249` 第 2 輪）
- [x] 美玲 → 總機（`alias-not-found`；2026-11-17：`1794904727.229`／`1794904757.232`／
      `1794904794.235`），**不是**掛線、**不是**轉給林志明。11-18 未重撥
- [x] 回歸（2026-11-17）：「請問小美在嗎？」也要問部門（`alias-ask`），不得因為殘留「好請問」而直轉 10XX
      （`1794988299.249`／`1794988529.255`／`1794988632.256`）
- [x] 回歸（2026-11-17）：「小米」→ 總機（`low-confidence-single`，0.51），不得轉 10XX（王小明）
      （`1794988179.243`／`1794988496.252`／`1794990193.276`，三次一致）
- [x] 401／403 → 播 `goodbye` 掛線（`reason=stt-auth`、`DBN_ACTION=hangup`），**不轉總機**
      （`1794988731.257`；本次用 `.env` 註解 key 製造，不是 drop-in）

401 的測法（不要動到正式 key；兩個路徑選一個）：

```bash
# systemd：用 drop-in 覆寫環境變數，不動 .env
sudo systemctl edit dial-by-name-agi      # [Service] 段加 Environment=ELEVENLABS_API_KEY=invalid
sudo systemctl daemon-reload && sudo systemctl restart dial-by-name-agi
# 測完
sudo systemctl revert dial-by-name-agi && sudo systemctl restart dial-by-name-agi
```

```bash
# scripts/agi.sh：load_env 會覆蓋指令列變數，所以只能暫時改 .env（記得備份）
cp .env .env.bak
sed -i '/^ELEVENLABS_API_KEY/s/=.*/=invalid/' .env
scripts/agi.sh restart
# 測完
mv .env.bak .env && scripts/agi.sh restart
```

若 drop-in 的 `Environment=` 沒有蓋過 `EnvironmentFile=`（少數 systemd 版本），改用 `.env` 備份法。
2026-11-18 的驗收直接在 `.env` 把 `ELEVENLABS_API_KEY` 註解掉（`#`）：空 key 送到 API 一樣是 401，
結果與 drop-in 相同，測完記得取消註解。

### 4.3 已完成但先前未記錄

- [x] `eval.py --calls` 8 通實撥：8/8 決策符合預期（2026-11-16）。`truth.csv` 與 wav 不進版控
      （見 README 的「量測」章節）

### 4.4 同音＋部門（已完成，2026-11-18）

342 題的 `homophone_dept_top1 1.000` 在真實音檔從未觸發，以三通補驗（2026-11-18）：

- [x] 「好，我要找客服部的王偉」→ 直轉 **10XX**（`1794989391.263`，u002 0.947）。部門收斂
      （`DEPT_SCOPE_MIN`）把業務部的王偉排除，只剩客服部的汪偉——**不是**「雙候選再問部門」
- [x] 「业务部王伟」→ 10XX（`1794989536.268`，u001 1.00）
- [x] 「资讯部王伟」→ 部門內無人相符 → `ask`（`1794989726.273` 第 1 輪，4 候選）→ 再答「王伟」
      → `clarify-exhausted` → 總機（第 2 輪），**不轉錯人**

## 5. 已知限制

- STT 期間（`processing` 播完到 AGI 回來）沒有任何輸入處理；按 0 要等下一輪錄音。
- **STT 全斷（非 401，而是 timeout）沒有 fast-fail**：要等兩次 12 秒 timeout 才掛線
  （約 24 秒純等待，最壞 50 秒；細節見 §3.9）。沒有熔斷、沒有退避。
- `retry` 的提示音（「沒聽清楚，請再說一次」）在 API 故障時是誤導的；改播 `goodbye`
  會撞到「adapter 不計次」的契約，所以維持現狀。
- STT 回 200 但聽成亂句**不算失效**：走正常決策，多半落在 `no-intent` → 掛線。
  除錯時要 `stt_status: "ok"` 配 `reason: "no-intent"` 一起看，才分得出「聽錯」與「壞掉」。
- 401／403 的觀測面只有 `out/calls/*.jsonl` 的 `stt_status`；沒有健康檢查、沒有告警。
  正式階段 STT 改 Realtime 後整套重做（見 `ROADMAP.md`），本節不展開設計。
- **「追問部門」的那一輪**沒有按 0 的逃生口（`DBN_REC_OPTS` 清空、不播 `q-operator`）。
  但「沒聽清楚」的重試錄音仍保留按 0：`retry` 不增加 `DBN_ATTEMPT`，屬於同一輪，
  而連話都說不出來的來電者只剩這條路。
  追問後聽不懂又答不出來的來電者會沉默兩次 → 掛線，不是轉總機；這是 2026-11-14 的刻意取捨。
- frontend socket idle 25 秒；handler 另有 25 秒 deadline（`agi_server.py` 用 thread join）。
  handler 超時會直接關閉連線；dialplan 是否拿到 `AGISTATUS=FAILURE` 需在實機用 `agi set debug` 驗證，
  不要把它當保證。
- 所有目的地（人、部門、總機）都用 `Local/<num>@from-internal`；分機、queue、ring group 不區分。
  這條路徑依賴 FreePBX 把 `[ext-queues]`（以及其他目的端）include 進 `from-internal`，標準安裝成立。
- queue 會立刻應答（播等候音樂），所以 `Dial` 的 `DIALSTATUS` 會是 `ANSWER`，**即使沒有 agent 接**。
  「`Dial` 失敗 → 總機」這個安全網對 queue 目的地不會觸發；之後由 queue 自己的 ring timeout 決定。
