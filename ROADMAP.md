# Roadmap

這份文件只寫**現況與下一步的摘要**；詳細設計理由在 `docs/design.md`，
變更歷史在 `CHANGELOG.md`。

## 現況

- 比對層、決策層、AGI 單輪處理器、FastAGI server、正式名冊入口（userman）都已完成並可離線驗證。
- 342 題合成測資＋20 通劇本（L0／L1）作為回歸閘門；`pre-commit` 會在改動核心路徑時先跑。
- 決策層零外部模型存取；`scripts/jev_probe.py scan` 作為重開訊號（現行語料可達率 0）。

## 下一步（依優先序）

1. **STT 改 Realtime（邊錄旁路）**：目前是「錄完一輪 → 送整檔」，改為通話中邊錄邊送，
   目標是縮短 `processing` 的空白。接線優先序：`Monitor` 分腿 → `MixMonitor` 備案；
   排除 AudioSocket（會丟 DTMF 逃生口）與 ARI。
2. **故障必須落到人**：STT 連不上／中斷／401 一律轉總機（目前 401 直接掛線是 PoC 立場），
   並補健康檢查與告警。沉默來電（有音框但無 commit）走重試，不灌總機。
3. **真實語料與指標**：用自己的 20–50 通錄音補 `eval.py --calls` 的指標；
   在那之前不對外宣稱任何準確率（合成測資只是回歸訊號）。
4. **keyterms A/B**：ElevenLabs keyterms 是否值得啟用（送簡體 vs 繁體）需要三組共用同一份
   truth.csv 的 A/B。
5. **`disclose_names=True`（內線專用）**：補 `q-is-it`／`q-or`／`q-question`／`transferring-name`
   四支固定句、逐人名音檔與 manifest，並在 `agi_server.py` 加旗標。
6. **Go 移植（可選）**：常數已集中在 `core.WEIGHTS`／門檻常數；建議離線建索引輸出 JSON，
   執行期只做「查表＋打分＋決策」，避免 CGO 依賴。

## 不做（已知的邊界）

- 不把整本電話簿送給任何外部服務（含重開外部模型時：一次最多 8 筆候選）。
- 不用按鍵盲選人（按鍵只選部門／總機）。
- 不在通話中即時查 LDAP；名冊靠 SIGHUP 重載。
