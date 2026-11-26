# 部署：從 clone 到第一通電話

本檔從 `git clone` 走到第一通電話：先期條件、部署樹、`.env`、音檔、dialplan、啟動與更新。開始前需要 FreePBX 22+、可 SSH 的 `asterisk` 帳號，以及 19 支提示音（repo 不含音檔）。

全程在**部署主機**上、cwd 一律部署樹；每一步的產物就是後續步驟引用的路徑。
本檔預設以 `asterisk` 帳號執行（`ssh asterisk@<pbx>`）：`/var/lib/asterisk`、`/etc/asterisk`
本為 asterisk 所有，不需要 sudo。決策層沒綠的版本不要接電話；開發端回歸見 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。

<a id="prereq"></a>
## 先期條件

開工前這九項要先成立。缺哪一項，對應那一步就會失敗——不是「先做，之後再補」。

| # | 條件 | 說明 |
|---|---|---|
| 1 | FreePBX＋Asterisk 22+ | dialplan 貼 `extensions_custom.conf`；名冊走 FreePBX userman（`AGI_PBX_DB=1`＋`AGI_SOURCE_DIRECTORY`） |
| 2 | `git`＋`uv` | `git --version && uv --version`。缺 `uv`：`curl -LsSf https://astral.sh/uv/install.sh \| sh`（裝完重開 shell） |
| 3 | 服務帳號 | **建議** `asterisk`（與 Asterisk 寫檔同帳號）；專用帳號是**可選**，代價見下節 |
| 4 | 操作帳號＝`asterisk` | 部署全程以 asterisk 直接操作；`fwconsole` 與補裝系統套件才需要 root |
| 5 | 對外 443（WSS 到 `api.elevenlabs.io`） | 唯一外部依賴；連不出去＝每通 `fallback` → **總機**（不是掛線） |
| 6 | 本機 `127.0.0.1:4573` | Asterisk→AGI 走 loopback、僅本機；不必開防火牆，也不要對外 listen |
| 7 | DB 唯讀存取 | `/etc/freepbx.conf` 可讀（建議 asterisk 帳號的預設路徑），或 `.env` 補齊 `DBN_MYSQL_*` 四鍵；兩邊都不成＝拒絕啟動 |
| 8 | FreePBX 設定面 | `DBN_OPERATOR` 的落點（總機分機／ring group）、Inbound Route／Custom Destination；貼法見 [`dialplan.md#snippet`](dialplan.md#snippet) |
| 9 | `ELEVENLABS_API_KEY` | 只寫進部署樹的 `.env`（第 4 步），不進指令行、不進版控 |

<a id="account"></a>
## 部署目錄與服務帳號

**目錄：建議 `/var/lib/asterisk/dial-by-name`**——兩場真機實撥走的就是這條路徑。可以換，但三條要同時成立：

1. 服務帳號對部署樹**可讀可執行**（`.venv/bin/python`、`dialname/`、`data/`、`scripts/`），
   對 `out/` 與 `recordings/` **可寫**；
2. 不在 Asterisk／FreePBX 自管路徑（`sounds/`、`moh/`）底下——那兩棵 FreePBX 會重寫；
3. `DBN_SOUND`／`DBN_OPERATOR` 等 dialplan 變數的地點對得上。

**帳號：建議 `asterisk`**。與 Asterisk 同帳號寫檔＝`recordings/` 零權限工程
（`Record()`／`MixMonitor()` 落的 wav，AGI 與 sweeper 直接可讀可刪），DB 走 `/etc/freepbx.conf` 也免設定。

**可選：專用帳號**（例如 `dbn`）。隔離更好，代價三條：

1. `recordings/` 要共享群組＋setgid（`sudo chgrp asterisk recordings && sudo chmod 2770 recordings`），
   否則 Asterisk 寫的 wav 服務刪不掉；
2. `.env` **必須**補 `DBN_MYSQL_*` 四鍵（專用帳號讀不到 `/etc/freepbx.conf`）；
3. 啟動改以該帳號登入（或 `sudo -u <帳號>`）。

## 部署主線（10 步）

<a id="clone"></a>
### 1. 部署樹：`git clone`

```bash
git clone <repo-url> /var/lib/asterisk/dial-by-name
cd /var/lib/asterisk/dial-by-name
git checkout <tag-or-commit>   # 選用：鎖版本，部署可重現
```

- 目標目錄必須**不存在或為空**（`git clone` 不進非空目錄）。
- 樹屬於操作帳號（本帳號 clone）：服務直接讀寫樹內自有目錄，不需 `chown`。
- `scripts/agi.sh` 用腳本位置推算根目錄，不依賴 cwd。

### 2. 依賴：`uv sync`

```bash
cd /var/lib/asterisk/dial-by-name
uv sync     # 產部署樹的 .venv/（依 pyproject.toml + uv.lock；不入版控）
```

`uv` 裝在 `~/.local/bin` 而 PATH 找不到時：`~/.local/bin/uv sync`（或先
`export PATH="$HOME/.local/bin:$PATH"`）。之後 `pyproject.toml`／`uv.lock` 有動就要重跑這一步再
`restart`，見第 10 步。

<a id="dirs"></a>
### 3. 執行目錄與權限

```bash
cd /var/lib/asterisk/dial-by-name
mkdir -p recordings out/calls
chmod -R 0770 recordings out
touch recordings/.probe out/calls/.probe
rm recordings/.probe out/calls/.probe
```

- Asterisk 的 `Record()`／`MixMonitor()` **不會自建目錄**：`recordings/` 不存在＝錄音失敗
  （`RECORD_STATUS=ERROR` → `retry` → 掛線）。
- `recordings/` 只放通話中的暫存 wav：走到 `result` 即刪，其餘由 sweeper 在 20 秒內清；
  靜止時必須是空的（有殘檔＝sweeper 沒在跑）。
- `out/calls/` 是 JSONL 落點（`AGI_LOGS` 預設）；`out/` 另有 `agi.log`／`agi.pid`／`agi.health.json`。
- `touch` probe 兩檔都要過；失敗＝服務寫不進去。

<a id="env"></a>
### 4. `.env`

```bash
cd /var/lib/asterisk/dial-by-name
tee .env >/dev/null <<'EOF'
ELEVENLABS_API_KEY=
AGI_PBX_DB=1
AGI_SOURCE_DIRECTORY="Test Directory"
DBN_MYSQL_HOST=127.0.0.1
DBN_MYSQL_USER=dbn_ro
DBN_MYSQL_PASS=
DBN_MYSQL_DB=asterisk
EOF
chmod 600 .env
```

- 金鑰不進指令行：先建檔再用編輯器填 `ELEVENLABS_API_KEY`。只寫 `KEY=value`，**不要加 `export`**
  （`scripts/agi.sh` 用 `set -a` 載入，值有空格要加引號）。
- `AGI_PBX_DB=1`＋`AGI_SOURCE_DIRECTORY`（換成你的 userman 目錄名，值要加引號）＝正式名冊；
  拿掉這兩行則退回 `data/people.json` 示範名冊（40 人，僅供離線開發）。
- `DBN_MYSQL_*` 四鍵全有才用；缺任一 fallback `/etc/freepbx.conf`（需 asterisk 可讀），
  兩邊都不齊或連線失敗 → 拒絕啟動。
- 用專用帳號時：`.env` 的 owner 改該帳號，且 `DBN_MYSQL_*` 四鍵**必填**。

<a id="aliases"></a>
### 5. `data/aliases.json`（userman 名冊的別名 sidecar）

```bash
cd /var/lib/asterisk/dial-by-name
cp data/aliases.example.json data/aliases.json
chmod 600 data/aliases.json
```

改成你的 `source_directory`（逐字等於 userman 目錄名）與 `user_id → 小名`。repo 刻意不含正式檔：
含真名，隱私。稀疏登記即可；離職未刪的孤兒 `user_id` 會讓 reload 永遠失敗。欄位與驗證規則見
[`decision-logic.md`](decision-logic.md)（alias 政策）。

<a id="sounds"></a>
### 6. 提示音 19 支 → `/var/lib/asterisk/sounds/dbn/zh/`

音檔是這條主線**唯一的外部輸入**：repo 不含、也無法由前面的步驟生成——在**開發端**錄製或合成
（每支要念什麼、何時播見 [`sounds.md`](sounds.md)），驗完格式再傳上部署主機。

```bash
# 開發端先驗格式（8 kHz／mono／16-bit、檔名 ASCII、19 支齊全）
uv run scripts/check_sounds.py <你的音檔目錄>

# 部署主機：目標目錄要先存在
mkdir -p /var/lib/asterisk/sounds/dbn/zh

# 開發端：上傳（音檔不進 repo，只能檔案傳輸）
scp <你的音檔目錄>/*.wav asterisk@<pbx>:/var/lib/asterisk/sounds/dbn/zh/
```

驗收：`check_sounds.py` 印 `OK: 19 個音檔齊全且格式正確`；
`ls /var/lib/asterisk/sounds/dbn/zh/*.wav | wc -l` 回 `19`。

repo 刻意不含音檔：真人錄音／TTS 資產的再散布條件另計。**`dbn/` 這層必須與 dialplan 的
`DBN_SOUND` 逐字一致**：少一層＝`Playback` 全無聲、且沒有明顯報錯。

<a id="dialplan"></a>
### 7. dialplan：貼上與 reload

片段本文、逐行語意與 GUI 接入在 [`dialplan.md#snippet`](dialplan.md#snippet)。
`[dbn-main]`／`[dbn-play-speech]` 貼進 Admin → Config Edit → `extensions_custom.conf`，然後：

```bash
asterisk -rx 'dialplan reload'
```

（FreePBX GUI 貼上的話，按 GUI 的 Apply Config 亦可；`fwconsole` 需要 root，`asterisk -rx`
以 asterisk 身分即可。）**不要**放進 `[from-internal-custom]`：它永遠被 include 進 `from-internal`，
`exten => s` 會變成內部分機可撥。

### 8. 啟動 FastAGI＋金絲雀

```bash
cd /var/lib/asterisk/dial-by-name
scripts/agi.sh start
tail -n 5 out/agi.log
```

金絲雀必須看到 `roster: source='…' people=N aliases=M departments=D`，且 `people=N` 對上名冊真實人數。
啟停細節、`status` 判定與人員異動見 [`operations.md`](operations.md)。

<a id="smoke"></a>
### 9. 冒煙一通

先跑 [`dialplan.md`](dialplan.md) §3.8 的兩項檢查（STT 對外、決策管線，不必撥號），
再用 Local channel 撥 `s@dbn-main`；正式進線走 Custom Destination／Inbound Route。
判讀看 JSONL 的 `DBN_ACTION`／`reason`。之後照 [`calls.md`](calls.md) 撥 20 通劇本。

<a id="update"></a>
### 10. 更新既有部署

```bash
cd /var/lib/asterisk/dial-by-name
git pull
scripts/agi.sh restart    # 改過程式碼／依賴／.env
scripts/agi.sh reload     # 只改名冊（userman／aliases.json）
```

- 依賴有變（`pyproject.toml`／`uv.lock` 有動）→ **先重跑第 2 步**再 `restart`。
  `.venv` 與 `uv.lock` 不一致時進程會 import 失敗，log 只看得到 traceback。
- 只改 `aliases.json`／`departments.json` → `reload` 就夠（SIGHUP 原子重載名冊，進行中的 session 不重置）。
