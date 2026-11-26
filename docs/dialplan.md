# Dialplan：AGI 只做 STT＋決策，其餘交給 FreePBX

本檔提供 `extensions_custom.conf` 的片段、`DBN_*` 通道變數與電話層失敗分派。

> 這份片段由使用者**手動**貼進 `/etc/asterisk/extensions_custom.conf`，agent 不遠端改 PBX。
> 規則：`;` 之後可以是中文註解，**其餘一律 ASCII**（`tests/test_dialplan_ascii.py` 會驗）。

> 章節編號沿用舊版：§1（部署）與 §4（PoC 驗收清單）已移除、§3.5／§3.6 已搬
> [`operations.md`](operations.md)；不重排是為了讓全 repo 既有指針仍有效。

## 目錄

- [§2 片段](#snippet)
- [§3 套用（GUI 接入）](#apply)
- [§5 已知限制（電話層語意）](#limits)

<a id="snippet"></a>
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

<a id="apply"></a>
## 3. 套用（GUI 接入）

> **順序**：貼上（§3.1）後、撥號前，先照 [`operations.md`](operations.md) 啟動 FastAGI、跑完 §3.8 冒煙。
> AGI 沒起來時撥號會走 `AGISTATUS` 安全網轉總機，很容易誤判成 dialplan 寫錯。
> 服務啟動、cwd、權限與人員異動見 [`operations.md`](operations.md)。

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

### 3.7 套用後驗證

```bash
sudo fwconsole reload
sudo asterisk -rx 'dialplan show dbn-main'
sudo asterisk -rx 'dialplan show dbn-play-speech'
sudo asterisk -rx 'dialplan show dbn-main@dbn-main'   # 應看到 s,1 Answer()
```

### 3.8 第一通之前的 AGI 冒煙（不必撥號）

**先確認服務活著**：照 [`operations.md`](operations.md) 啟動；`scripts/agi.sh status` 的
PID／4573／sweeper heartbeat 都要 OK（sweeper DEAD 會回非零），`ss -ltnp | grep 4573`
應看到 listener，`out/agi.log` 有 `FastAGI listening on 127.0.0.1:4573`。

**STT 對外通不通**（key 有效、外網可達、模型可呼叫；音檔位置見 [`deploy.md#sounds`](deploy.md#sounds)）：

```bash
uv run stt_batch.py --audio /var/lib/asterisk/sounds/dbn/zh/welcome.wav
```

**決策管線通不通**（不經電話、不經協議層，直接呼叫 handler）：

```bash
uv run python -c "
from dialname.core import load_directory
from dialname.stt_client import SttClient
from dialname.agi import handle_turn
d = load_directory('data/people.json')
print(handle_turn('/var/lib/asterisk/sounds/dbn/zh/welcome.wav', 1, stt=SttClient(timeout=12.0), directory=d))
"
```

判讀：`welcome` 的內容是「請說出您要找的部門或姓名」，含路由意圖詞「找」，
所以**預期得到 `action=ask`／`reason=no-candidate-retry`**（有找人意圖但沒有候選）。
出現 `retry` 代表 STT 回空字串或呼叫失敗（音檔、key、網路）；`hangup`／`no-intent`
代表意圖詞判定異常；`route`／`route_dept` 代表名冊或比對異常。
三項都對再撥號：第一通可用 Local channel 撥 `s@dbn-main` 演練，不佔外線；
判讀看 JSONL 的 `DBN_ACTION`／`reason`（落點見 §3.9）。

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

**`auth` 與 `ws` 的判別**：記法以 [`agi-protocol.md#errors`](agi-protocol.md#errors) 的「STT 錯誤分類」為唯一來源
（`stream.error`／`stream.detail` 與 2026-11-25 裁定）；行為面兩者一律
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

STT 回 200 但聽成亂句**不算失效**：走正常決策，多半落在 `no-intent` → 掛線。
除錯時要 `stt_status: "ok"` 配 `reason: "no-intent"` 一起看，才分得出「聽錯」與「壞掉」。

**wav 即刪**：`result` 走到就刪旁路檔與 `Record` 檔；沒走到 `result` 的分支
（OPERATOR／HANGUP／ERROR／閘滿 stub）由 sweeper 在 TTL（20 秒）到期時刪。
兩條路徑都只刪 session 登記的兩檔，JSONL 的 `wav` 檔名保留（join key）。

<a id="limits"></a>
## 5. 已知限制（電話層語意）

- **收音期沒有 barge-in**：旁路只監聽，來電者仍不能在提示音期間打斷。
  `Playback(processing)` 之後（等 `result`，最多 3 秒）也沒有輸入處理；按 0 要等下一輪錄音。
- **VAD commit 與檔案 flush 的粒度**：`MixMonitor` 的 side file 是 32 KiB（8 kHz s16＝2.048 秒）
  一批才落盤；若最後一段語音的 commit 落在未滿一批的尾巴，`StopMixMonitor` 才吐出，
  `result` 要多等一次 ASR commit（約 150–400 ms）。成功標準 #1 的 p95 < 300 ms 有風險，
  R3 要用 `stream.commit_ms` 分佈驗；常態輪（先停話）不受影響。
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
