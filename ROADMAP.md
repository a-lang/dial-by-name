# Roadmap

這份文件只寫**現況與下一步的摘要**；詳細設計理由在 `docs/decision-logic.md`，
變更歷史在 `CHANGELOG.md`。

## 現況

- 比對層、決策層、AGI 單輪處理器、FastAGI server、正式名冊入口（userman）都已完成並可離線驗證。
- Realtime 邊錄旁路已完成（`MixMonitor` 分腿、`start` → `result` 兩相）；`Record(...,4,10,...)`
  與 beep／靜音／`#`／按 0 語意不變。
- 401／403／WS 連不上或中斷一律 `fallback` → 總機（2026-11-23 定案）；掛線只留給無意圖／無回應。
- health heartbeat 已有（`out/agi.health.json` ＋ `scripts/agi.sh status`）；告警刻意不做。
- 342 題合成測資＋20 通劇本（L0／L1）作為回歸閘門；`pre-commit` 會在改動核心路徑時先跑。
- 決策層零外部模型存取；`scripts/jev_probe.py scan` 作為重開訊號（現行語料可達率 0）。

## 下一步（依優先序）

1. **真實語料與指標**：用自己的 20–50 通錄音補 `eval.py --calls` 的指標；
   在那之前不對外宣稱任何準確率（合成測資只是回歸訊號）。
2. **keyterms A/B**：ElevenLabs keyterms 是否值得啟用（送簡體 vs 繁體）需要三組共用同一份
   truth.csv 的 A/B。
3. **`disclose_names=True`（內線專用）**：補 `q-is-it`／`q-or`／`q-question`／`transferring-name`
   四支固定句、逐人名音檔與 manifest，並在 `agi_server.py` 加旗標。
4. **Go 移植（可選）**：常數已集中在 `core.WEIGHTS`／門檻常數；建議離線建索引輸出 JSON，
   執行期只做「查表＋打分＋決策」，避免 CGO 依賴。

## 不做（已知的邊界）

- 不把整本電話簿送給任何外部服務（含重開外部模型時：一次最多 8 筆候選）。
- 不用按鍵盲選人（按鍵只選部門／總機）。
- 不在通話中即時查 LDAP；名冊靠 SIGHUP 重載。
