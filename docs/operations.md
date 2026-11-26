# 維運：啟停、健康與名冊

本檔說明上線後的啟停、健康判定、名冊異動與故障處理。

正式運行模式是**手動** `scripts/agi.sh`（2026-11-25 裁定不實裝 systemd）：**重開機不會自動起服務**，
開機後要手動 `scripts/agi.sh start`。systemd 是選用路，見下節，兩者**不可同時跑**（port 衝突）。

<a id="start"></a>
## 啟停：`scripts/agi.sh`

```bash
cd /var/lib/asterisk/dial-by-name

# 啟動／停止／重啟（.env 已含 userman 設定：AGI_PBX_DB=1、AGI_SOURCE_DIRECTORY）
scripts/agi.sh start
scripts/agi.sh restart       # 改程式碼、換 AGI_LOGS、改 .env 後用
scripts/agi.sh reload        # SIGHUP：原子重載名冊（userman／aliases.json 異動後用；不換程式碼）
scripts/agi.sh stop
scripts/agi.sh foreground    # 前景執行，Ctrl+C 停止、log 直接看

# 狀態（可腳本判定：running 但 sweeper 不健康 → 非零退出碼）
scripts/agi.sh status

# 驗收場次專屬日誌（撥打前設置，勿與生產日誌共用）
AGI_LOGS=out/calls/test-YYYYMMDD scripts/agi.sh restart
```

每次啟動／重啟後的金絲雀：

```bash
tail -n 5 out/agi.log   # 必須看到 roster: source='Test Directory' people=N aliases=M departments=D
```

指令之外的語意：

- `foreground`＝前景執行，Ctrl+C 停止、log 直接看；背景模式 PID 寫 `out/agi.pid`、log 寫 `out/agi.log`。
- 預設 listen `127.0.0.1:4573`；換埠 `AGI_LISTEN=127.0.0.1:4574 scripts/agi.sh start`。
- 換名冊（臨時驗收）：`AGI_DIRECTORY=/path/to/roster.json scripts/agi.sh start`。
  **systemd 不讀這個變數**——那條路的 `--directory` 寫在 unit 的 `ExecStart`。
- userman 模式：`AGI_PBX_DB=1 AGI_SOURCE_DIRECTORY='<你的 directory 名>' scripts/agi.sh start`
  （`AGI_ALIASES`／`AGI_DEPARTMENTS` 可換路徑）；回滾＝拿掉 `AGI_PBX_DB` 重啟。
- 名冊旗標（手動與 systemd 的 `ExecStart` 同一組）：
  `--pbx-db --source-directory '<你的 directory 名>' --aliases data/aliases.json --departments data/departments.json`。
- `.env` 存在會自動 source（值會蓋掉 inline 環境變數）：key 不進指令行、不進 shell 歷史；
  其他參數原樣傳給 `agi_server.py`（例 `--health out/agi.health.json`）。
- `reload` 只支援 `start` 的背景模式（要有 `out/agi.log`）：送 HUP 後等 10 秒，
  抓最新一筆 `reload ok|failed`；`reload failed` 時服務仍在跑（舊索引），退出碼 1。
  SIGHUP 只換名冊，**進行中的 session 不重置**（`TurnManager` 跨 reload 存活）。

<a id="health"></a>
## 健康判定

`scripts/agi.sh status` 三件事都看：

| 面 | 來源 | 判定 |
|---|---|---|
| 行程 | `out/agi.pid`（只有 `agi.sh start` 會寫） | PID 不存在＝`stopped`；systemd 起的進程在這裡也是 `stopped` |
| 監聽 | `ss -ltnp` 的 4573 | 服務在 listen 才可能接得到 AGI |
| sweeper | `out/agi.health.json`（每 **1 秒**寫一次，原子換檔） | heartbeat 超過 **5 秒**沒更新／pid 不符／檔不存在 → `sweeper: DEAD`，退出碼 1 |

`sweeper: DEAD` 不是加分項：sweeper 死掉時行程與 port 可能都還在，但過期 session 會永佔並發閘
（4 個），之後新話務全落總機。`active` 是當下的 session 數，`jsonl:` 是最新一輪的 `ts`。
`AGI_HEALTH` 可換 heartbeat 路徑（預設 `out/agi.health.json`）。輸出長相：

```text
running: pid 4242  listen 127.0.0.1:4573
LISTEN 0      128                 127.0.0.1:4573       0.0.0.0:*
sweeper: alive  age=0.4s  active=0  last_sweep=2026-11-26T10:00:00+08:00
jsonl: 2026-11-26T09:59:58+08:00  /var/lib/asterisk/dial-by-name/out/calls/20261126.jsonl
```

<a id="roster"></a>
## 人員異動（6 步）

1. userman 改人／外部目錄同步：`sudo fwconsole userman --syncall --force`
   （FreePBX 管理指令需 root；AD/LDAP 先拉進 DB）。userman 本身**不需要** Apply Config：
   姓名、部門、分機綁定只寫 `userman_users`，AGI 直接 SELECT。例外是同一批動到分機（Extensions，
   含 Quick Create 建的使用者）、queue、ring group、inbound route，或改了 `extensions_custom.conf`——
   那才需要 reload dialplan（`asterisk -rx 'dialplan reload'` 或 GUI 的 Apply Config），
   漏了它 `Dial(Local/<ext>@from-internal)` 會找不到新分機。
2. 離職：同步刪掉 `data/aliases.json` 裡該 `user_id`（孤兒 alias 會讓 reload 永遠失敗）。
3. `uv run scripts/check_aliases.py --source-directory '<你的 directory 名>'` 退出碼 0。
4. `scripts/agi.sh reload` → 必須看到 `reload ok: people=N aliases=M (was K)`；
   `reload failed` 時服務仍用舊索引，修好再 reload。進行中的 session 不重置。
5. `aliases.json`／`departments.json` 一律「寫暫存檔 → `mv` 取代」，避免半寫入碰上 HUP。
6. 金絲雀：`tail -n 5 out/agi.log` 的 `people=N` 要等於變更後的人數。

<a id="key-failure"></a>
## 故障演練：STT key 失效

```bash
cd /var/lib/asterisk/dial-by-name
cp -p .env /tmp/dbn.env.bak
sed -i '/^ELEVENLABS_API_KEY=/s/=.*/=/' .env   # 清空值，不刪行
scripts/agi.sh restart
# 撥一通 → out/calls/YYYYMMDD.jsonl 該輪應見 "stt_status":"auth"、DBN_ACTION=fallback → 總機
mv /tmp/dbn.env.bak .env && scripts/agi.sh restart
```

`auth`（握手 401／403 或缺 key）與 `ws`（WS 事件層錯誤）都落 `fallback` → 總機；
分類的權威是 [`agi-protocol.md#errors`](agi-protocol.md#errors)。

<a id="systemd"></a>
## 選用：systemd（開機自啟）

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
grep "roster: source" out/agi.log | tail -1    # 金絲雀：people=N
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
- 裝 systemd 前先 `scripts/agi.sh stop`；裝了之後不要再手動 `start`（兩邊搶 4573）。
- `--source-directory` 是 unit 裡的死值：換 userman 目錄名要改 unit 或加 drop-in，再 `daemon-reload`。

<a id="logs"></a>
## 日誌位置與排障入口

| 位置 | 內容 |
|---|---|
| `out/agi.log` | 啟動、`roster:`、`reload ok/failed`、traceback |
| `out/calls/*.jsonl` | 每輪逐字稿＋決策（`stt_status`／`stream.status`／`DBN_ACTION`／`reason`） |
| `out/agi.health.json` | sweeper heartbeat（每 1 秒） |
| `out/agi.pid` | 背景模式 PID（只有 `agi.sh start` 會寫） |
| `recordings/` | 通話中暫存 wav；靜止時應為空 |

排障順序：

1. **撥了沒反應或落總機**：先看該輪 JSONL 的 `DBN_ACTION`／`reason`／`stt_status`；
   `auth`／`ws` 與失敗分派的權威是 [`agi-protocol.md#errors`](agi-protocol.md#errors)。
2. **`roster:` 人數不對**：查 `AGI_SOURCE_DIRECTORY` 是否逐字等於 userman 目錄名、userman 是否已同步。
3. **`reload failed`**：服務還在跑舊索引；先跑 `check_aliases.py`，修好再 reload。
4. **`sweeper: DEAD`**：重啟服務；殘留 wav 不會自己消失，需人工清 `recordings/`。
5. **更新後 import 失敗**：`.venv` 與 `uv.lock` 不一致——重跑 `uv sync` 再 `restart`
   （[`deploy.md#update`](deploy.md#update)）。
6. **要整套驗收**：20 通劇本照 [`calls.md`](calls.md) 撥與判；`eval.py --calls`
   吃 JSONL＋`truth.csv` 算指標，見 [`evaluation.md#call-tests`](evaluation.md#call-tests)。
