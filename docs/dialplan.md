# Dialplan：AGI 只做 STT＋決策，其餘交給 FreePBX

> 角色：本檔是「PBX 部署、目錄、音檔、dialplan 貼法、驗收清單、人員異動 SOP」的唯一來源；跨檔只指向、不複製。

> 這份片段由使用者**手動**貼進 `/etc/asterisk/extensions_custom.conf`，agent 不遠端改 PBX。
> 規則：`;` 之後可以是中文註解，**其餘一律 ASCII**（`tests/test_dialplan_ascii.py` 會驗）。

## 1. 套用前準備

| 項目     | 指令／說明                                                                                                            |
| -------- | --------------------------------------------------------------------------------------------------------------------- |
| 音檔     | 本 repo **不含音檔**：自備 19 支提示音（8 kHz／mono／16-bit、檔名 ASCII），先 `uv run scripts/check_sounds.py <暫存目錄>` 驗證，再 `rsync -av <暫存目錄>/ asterisk@my-ipbx2025:/var/lib/asterisk/sounds/dbn/zh/`——**`dbn/` 這一層不能省**，它必須與 §2 的 `DBN_SOUND` 逐字一致（少一層＝`Playback` 全無聲且無明顯報錯） |
| `.env`   | 專案根目錄；`ELEVENLABS_API_KEY`（＋ userman 模式的 `DBN_MYSQL_*`）；`chmod 600`、不進版控（見 §1.1）                                |
| 錄音目錄 | `mkdir -p /var/lib/asterisk/dial-by-name/recordings && chown asterisk:asterisk … && chmod 0770 …`（服務在 `result` 或 TTL 到期時刪 wav；目錄只用來放暫存檔）                             |
| 日誌目錄 | `mkdir -p /var/lib/asterisk/dial-by-name/out/calls && chown <agi-user>:asterisk …`                                            |
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
/var/lib/asterisk/dial-by-name/.env
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

收音期間的旁路只有三行（`start` AGI／`MixMonitor(,r(...))`／無條件 `StopMixMonitor`），
`Record(${DBN_WAV}.wav,4,10,${DBN_REC_OPTS})` 一字不動；beep、4 秒靜音、10 秒上限、
`#`、按 0、attempt／retry 的語意全部照舊。`start` 立即返回且不設通道變數，
決策與六個 `DBN_*` 全在 `result`（`Playback(processing)` 之後）。

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
 same => n,AGI(agi://127.0.0.1:4573,start,${DBN_WAV}-in.wav,${UNIQUEID},${DBN_SEQ})
 same => n,MixMonitor(,r(${DBN_WAV}-in.wav))
 ; silence=4s、max=10s、DBN_REC_OPTS=o 是按 0 轉總機（第二輪會清空）。不加 q 才會播 beep。
 same => n,Record(${DBN_WAV}.wav,4,10,${DBN_REC_OPTS})
 same => n,StopMixMonitor()
 same => n,GotoIf($["${RECORD_STATUS}"="OPERATOR"]?operator)
 same => n,GotoIf($["${RECORD_STATUS}"="HANGUP"]?done)
 same => n,GotoIf($["${RECORD_STATUS}"="ERROR"]?retry)
 same => n,Playback(${DBN_SOUND}/processing)
 same => n,AGI(agi://127.0.0.1:4573,result,${DBN_WAV}.wav,${UNIQUEID},${DBN_SEQ},${DBN_ATTEMPT},${DBN_CANDS})
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

> **順序**：先照 `README.md`「服務管理與開機自啟」啟動 FastAGI、跑完 §3.8 冒煙，再貼 dialplan（§3.1）。
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

### 3.5 FastAGI 服務（手動模式為正式；systemd 選用）

`agi_server.py` 必須常駐，且 **cwd 必須是專案根目錄**，否則相對路徑（`data/people.json`、
`out/calls`）會出錯（`scripts/agi.sh` 會自己 `cd`）。

2026-11-25 裁定**不實裝 systemd unit**：正式模式是手動 `scripts/agi.sh`（重開機後手動
`scripts/agi.sh start`）。啟動／停止／`status`／`reload`、`AGI_*` 與 userman 名冊旗標、
sweeper health 判定、systemd unit 全文與安裝驗證，一律以 `README.md` 的
「服務管理與開機自啟」為**單一來源**；本文件不重複。

`--pbx-db` 的 DB 設定讀 `.env` 四變數（缺任一才 fallback `/etc/freepbx.conf`）；
兩邊都不齊或連線失敗＝啟動失敗。source directory 打錯／0 人同樣拒絕啟動——
不要讓「空名冊成功啟動」把每一通電話靜默轉總機。

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
4. `scripts/agi.sh reload`（選用 systemd 時 `sudo systemctl reload dial-by-name-agi`）→ 必須看到
   `reload ok: people=N aliases=M (was K)`；`reload failed` 時**服務仍用舊索引**，修好再 reload。
5. `aliases.json`／`departments.json` 一律「寫暫存檔 → `mv` 取代」，避免半寫入檔案碰上 HUP。

**權限（服務身分與目錄必須一致）**

- 方案 A（本範例）：以 `asterisk` 身分跑（手動 `sudo -u asterisk scripts/agi.sh start`，
  或 systemd `User=asterisk`）；`.env` 必須是 `asterisk:asterisk 600`，
  專案目錄可被 asterisk 讀取，`recordings/` 與 `out/calls/` 都是 `asterisk:asterisk 0770`：
  `sudo chown asterisk:asterisk .env && sudo chmod 600 .env`
- 方案 B（僅選用 systemd）：`User=pbx`、`Group=asterisk`、`SupplementaryGroups=asterisk`；
  `.env` 是 `pbx:pbx 600`；`recordings/`／`out/calls/` 給 `asterisk:asterisk 0770`（pbx 靠群組讀寫）。

### 3.6 手動啟動（正式模式）

手動模式是**正式模式**，重開機不會自動起。`foreground`／`start`／`stop`／`restart`／`status`／
`reload` 的指令與語意、`AGI_*` 參數、health 判定與 systemd（選用）：全部見
`README.md`「服務管理與開機自啟」。**不要和 systemd 同時跑**（port 衝突）。

### 3.7 套用後驗證

```bash
sudo fwconsole reload
sudo asterisk -rx 'dialplan show dbn-main'
sudo asterisk -rx 'dialplan show dbn-play-speech'
sudo asterisk -rx 'dialplan show dbn-main@dbn-main'   # 應看到 s,1 Answer()
```

### 3.8 第一通之前的 AGI 冒煙（不必撥號）

**先確認服務活著**：照 `README.md`「服務管理與開機自啟」啟動；`scripts/agi.sh status` 的
PID／4573／sweeper heartbeat 都要 OK（sweeper DEAD 會回非零），`ss -ltnp | grep 4573`
應看到 listener，`out/agi.log` 有 `FastAGI listening on 127.0.0.1:4573`。

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
三項都對再進 §4 撥號。冒煙通過後的第一通可用 Local channel 撥 `s@dbn-main` 演練，不佔外線，
判讀同 §4。

### 3.9 STT 失效時會怎樣（Realtime）

生產路徑的失敗分流在 `handle_result()`（`dialname/agi.py`）：先看串流錯誤，再看
frames，再看 VAD partial，最後才是文字。**決定何時掛線的仍然只有 dialplan 的
`DBN_TRIES`**；adapter 不判斷「第二次空字串就掛線」。
（`handle_turn()` 只保留給離線工具／批次路徑，它的 401→`hangup` 不套用到電話。）

| 情況 | `DBN_ACTION` | `DBN_REASON` | JSONL `stt_status` | 來電者聽到 | 誰決定 |
|---|---|---|---|---|---|
| WS 連不上／401／403／中斷且無 transcript | `fallback` | `stream-error` | `auth`／`unavailable`／`ws`／`dropped`／`timeout`／`internal` | `to-operator` → 總機 | AGI（**政策翻案**：系統故障必須落到人） |
| WS 中斷但已有 transcript | 用現有文字照常 decide | 依決策 | `ok`；`stream.status=dropped` | 依決策 | AGI（已付的語音不浪費） |
| `frames=0`（旁路檔沒出現／tailer 死） | `fallback` | `bypass-empty` | `frames-0` | 總機 | AGI（系統故障） |
| `result` 找無 session（start 崩潰／服務重啟） | `fallback` | `result-no-session` | `no-session` | 總機 | AGI（系統故障） |
| `result` 見到 stub（並發閘滿） | `fallback` | `session-stub` | `stub` | 總機 | AGI（過載保護走人） |
| 無 commit 且全程無 partial（沉默／秒按 `#`） | **`retry`** | `no-speech` | `no-speech` | `retry`（沒聽清楚，請再說一次） | AGI；`DBN_TRIES` 用盡 → `hangup`＋`goodbye` |
| 無 commit 但曾有 partial（語音有進沒落地） | `fallback` | `stream-stalled` | `partial-only` | 總機 | AGI（串流落後視同故障） |
| 空逐字稿（有 commit、文字為空） | `retry` | `stt-empty` | `empty` | `retry` | AGI |
| 未預期例外（程式錯誤） | `fallback` | `internal-error` | `exception` | 總機 | AGI |
| 回傳值含非 ASCII／控制字元 | `fallback` | `non-ascii` | — | 總機 | AGI（`_ascii_result`） |
| `retry` 第二次（`DBN_TRIES` > 1） | `hangup` | — | — | `goodbye` → 掛斷 | **dialplan** |
| AGI 沒回應／超過 25 秒 | （無） | — | — | 總機 | dialplan（`AGISTATUS`） |

**`auth` 與 `ws` 的判別**：以 `docs/io-mapping.md` §3 的「STT 失敗的 auth 映射」為唯一來源
（`stream.error`／`stream.detail` 的記法與 2026-11-25 裁定）；本節只列行為面——兩者一律
`fallback` → 總機。

**判別訊號是 VAD partial，不是 frames**：旁路是 audiohook 連續串流，純靜音也會讓
`frames > 0`；「無 commit」時看的是「有沒有講過話」——全程無 partial ＝ 來電者沒說話
（`retry`），曾有 partial ＝ 串流落後（`fallback`）。`frames` 只用來抓「檔案沒出現」
這種系統故障，且只能在 `result` 判定（`StopMixMonitor` 後必定至少 flush 一次）。

`Playback(processing)` 的位置不動，`result` 端總預算是 **3 秒**（`RESULT_TIMEOUT`），
與 tailer／WS 的 EOF 收尾並行，不是串行疊加。常態輪的 transcript 在收音期已 commit，
等待接近 0；沉默輪才會付一次「等不到 commit」的 3 秒。舊版「兩次 12 秒 timeout＝至少
24 秒純等待」的描述作廢。

三條語意（改動前先讀）：

1. `retry` **不增加** `DBN_ATTEMPT`：它是同一輪重錄，不是追問；按 0 的逃生口也還在。
2. `DBN_TRIES` **整通電話共用、上限 1**：第一次空用掉重試後，之後任何一次空（不論 `attempt`）
   都掛線。`RECORD_STATUS=ERROR`（錄音本身失敗）走同一條路，**吃掉同一份預算**。
3. **系統故障 → 總機、來電者沒說話 → retry、無意圖／無回應 → 掛線**。401 不再掛線
   （PoC 的「key 壞掉不灌爆總機」已於 2026-11-23 翻案）；故障觀測面是 JSONL 的
   `stt_status`／`stream.status`，以及 `scripts/agi.sh status` 的 sweeper liveness。

**wav 即刪**：`result` 走到就刪旁路檔與 `Record` 檔；沒走到 `result` 的分支
（OPERATOR／HANGUP／ERROR／閘滿 stub）由 sweeper 在 TTL（20 秒）到期時刪。
兩條路徑都只刪 session 登記的兩檔，JSONL 的 `wav` 檔名保留（join key）。

## 4. 驗收清單（每次重新部署都要重跑）

`[ ]` 是可重複使用的模板：每次重新部署、改 dialplan 或改比對／決策邏輯後整份重勾，
任一項失敗就回滾到上一個版本。清單只寫「驗什麼」；失敗時各分支的落點
（`DBN_ACTION`／`stt_status`／誰決定）一律以 §3.9 為準。

歷史：2026-11-16 首次驗收 16 項通過（FreePBX 17.0.33.2、Asterisk 22.4.1、名冊 40 人、alias 0 筆）；
2026-11-17／18 alias 政策回歸通過（當次抓到並修正兩個 alias bug）。

### 4.1 首次上線

- [ ] §1.2 環境檢查（`uv sync`、`chmod +x scripts/agi.sh`、key 都印 `set`）
- [ ] §3.8 冒煙的判讀符合預期
- [ ] 從 Inbound Route（或內部分機 `*8000`）進線，聽得到 `welcome`（無聲先查音檔路徑與 `PLAYBACKSTATUS`）
- [ ] 說完一句後 `out/calls/YYYYMMDD.jsonl` 有紀錄（`wav` 欄位是 join key；旁路檔在
      `result`／TTL 即刪，不落地留存）
- [ ] `route` 轉到正確分機；`Dial` 失敗的落點符合 §3.9（queue 目的地不觸發）
- [ ] 只說「業務部」→ `route_dept`、播 `transferring&dept-sales`
- [ ] 連續兩輪不出聲的收尾符合 §3.9（`DBN_TRIES` 上限）
- [ ] 說「我要談業務合作」→ 追問部門（`dept-scoped-ask`）；第二輪仍無人 → 總機
- [ ] 「我要找陳美玲」→ 追問部門 → 答「客服部」轉 10XX；答錯部門 → 不得轉錯部門
- [ ] 按 0 的有效範圍：錄音期與澄清句（`Background`）有效；`welcome`／`processing`（`Playback`）期間無效
- [ ] 第一輪錄音按 0 → 總機；「追問部門」的那一輪按 0 無效
- [ ] 「沒聽清楚」的重試錄音仍可按 0
- [ ] 錄音前聽得到 beep（找不到 `/var/lib/asterisk/sounds/en/beep.*` 就得改回自錄 cue）
- [ ] `recordings/` 與 `out/calls/` 的權限 probe 成功
- [ ] `DBN_OPERATOR` 的落點正確
- [ ] AGI 掛掉時撥號的落點符合 §3.9（`AGISTATUS`）

### 4.2 alias 政策

`data/people.json` 內嵌 3 個示範 alias（阿明／小美／筱美），下表三個情境直接跑預設名冊就
驗得到——**不需要換名冊**。（正式 userman 模式沒有 alias 欄位，由 sidecar `aliases.json`
提供，key＝`user_id`；JSON 示範名冊仍內嵌 alias 供離線開發與本節驗收。）

| 情境 | 念 | 預期 |
|---|---|---|
| 唯一命中 | 阿明 | `route` **10XX**（林志明），`reason=direct:confident` |
| 同音多人 | 小美 | `ask`（`alias-ask`）→ 答「客服部」→ `route` **10XX**；答「行政部」→ **10XX** |
| 未登記 | 美玲 | `fallback`（`alias-not-found`）→ **總機**；不得轉給林志明 |

> 分機一律寫成 `10XX`（範例）。示範名冊（`data/people.json`）給的是具體值；換成你的名冊時，
> 只要「決策正確、分機指向該人」就算通過。

- [ ] 阿明 → 直轉 10XX（`direct:confident`；JSONL 的 candidates 只有 `pid`／`score` 可對）
- [ ] 小美 → 問部門（`alias-ask`）→ 答客服部／行政部都轉到對應的人；答錯部門 → 總機
- [ ] 美玲 → 總機（`alias-not-found`）
- [ ] 回歸：「請問小美在嗎？」也要問部門（`alias-ask`），不得因殘留「好請問」而直轉
- [ ] 回歸：「小米」→ 總機（`low-confidence-single`），不得轉 10XX（王小明）
- [ ] 401／403（含 WS `auth_error`）→ `fallback` → 總機（分類見 `docs/io-mapping.md` §3）

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

若 drop-in 的 `Environment=` 沒有蓋過 `EnvironmentFile=`（少數 systemd 版本），改用 `.env` 備份法；
測完記得還原並重啟。

### 4.3 `eval.py --calls`

- [ ] 實撥場次的決策與 `truth.csv` 全部吻合；`truth.csv` 與 wav 不進版控（見 README「量測」）

歷史：2026-11-16 首場 8 通 8/8。

### 4.4 同音＋部門

342 題的 `homophone_dept_top1` 在真實音檔從未觸發，以三通補驗：

- [ ] 「好，我要找客服部的王偉」→ 直轉 10XX：部門收斂（`DEPT_SCOPE_MIN`）排除業務部的
      王偉，只剩客服部的汪偉
- [ ] 「业务部王伟」→ 10XX
- [ ] 「资讯部王伟」→ 部門內無人相符 → `ask` → 再答「王伟」→ `clarify-exhausted` → 總機

歷史：2026-11-18 三通通過。

## 5. 已知限制

- **收音期沒有 barge-in**：旁路只監聽，來電者仍不能在提示音期間打斷。
  `Playback(processing)` 之後（等 `result`，最多 3 秒）也沒有輸入處理；按 0 要等下一輪錄音。
- `result` 端總預算 3 秒（`RESULT_TIMEOUT`）。WS 卡在握手就是等滿 3 秒才落總機；
  沒有熔斷、沒有指數退避（上線工程最小化）。
- **VAD commit 與檔案 flush 的粒度**：`MixMonitor` 的 side file 是 32 KiB（8 kHz s16＝2.048 秒）
  一批才落盤；若最後一段語音的 commit 落在未滿一批的尾巴，`StopMixMonitor` 才吐出，
  `result` 要多等一次 ASR commit（約 150–400 ms）。成功標準 #1 的 p95 < 300 ms 有風險，
  R3 要用 `stream.commit_ms` 分佈驗；常態輪（先停話）不受影響。
- **並發閘 4（寫死）**：四個沒走到 `result` 的 session 在 20 秒 TTL 內佔滿閘，期間新話務
  全量落總機（availability cliff）。R3 觀察 active session 數後再議「閘只計 streaming 態」。
- **TTL 20 秒＝隱私窗口**：OPERATOR／HANGUP／ERROR／閘滿 stub 的 wav 由 sweeper 在 20 秒內刪；
  服務若被 `kill -9`，sweeper 也停止，殘留檔沒有外部清理器（R3 稽核只涵蓋正常離開路徑）。
  `scripts/agi.sh status` 以 heartbeat 新舊判 sweeper 死活（超過 5 秒＝DEAD），不引進 metrics。
- STT 回 200 但聽成亂句**不算失效**：走正常決策，多半落在 `no-intent` → 掛線。
  除錯時要 `stt_status: "ok"` 配 `reason: "no-intent"` 一起看，才分得出「聽錯」與「壞掉」。
- 故障觀測面是 `out/calls/*.jsonl` 的 `stt_status`／`stream.status` 與 `scripts/agi.sh status`；
  沒有告警、沒有 metrics 匯出（刻意不做）。
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
