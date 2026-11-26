# Changelog

本檔記錄對使用者可見的變更：改了什麼、為什麼、對指標的影響。
本專案目前**不發佈版本號**，以日期（`YYYY-MM-DD`）分組；同一日的變更合併在一節。
類型沿用 Added／Changed／Removed／Fixed／Security；格式參考
[Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，但不採 Semantic Versioning。

純內部重構不必逐條列出；變更行為（決策、門檻、名冊）仍必須寫**改了什麼、為什麼、對指標的影響**。

## 2026-11-26

### Changed

- 文件資訊架構重寫：README 首屏改為決策導向，docs 重整為 10 份單一職責文件（`deploy`／`operations`／
  `architecture`／`decision-logic`／`agi-protocol`／`sounds`／`evaluation` 等）與 `CONTRIBUTING.md`；
  `AGENTS.md` 只留 agent 契約與地圖。部署主線只走 `git clone`，不進版控的資產（音檔、`.env`、
  `aliases.json`）以 `scp` 傳輸或在主機上建立。`CHANGELOG.md` 改為日期分組、不再推進版本號。
  不改程式行為，342 題指標與 20 通劇本不變。

### Security

- `.gitignore` 補上 `data/aliases.json`、`sounds/zh/*.wav`、`sounds/zh/manifest.json`、`recordings/`
  ——正式名冊與音檔含真名／再散布限制，repo 只留範本。

## 2026-11-25

### Added

- 專案封面圖 `assets/cover.html`（中文，骨白 × 硃砂）與 `assets/cover-en.html`（English，深藍底），
  各附 2560×1440 的 `cover.png`／`cover-en.png`。以 diagram-design（type-process 語法）繪製的
  四層 × 五階段流程圖（電話層／AGI 服務／決策核心／名冊 × 收音・轉錄・收斂・決策・執行），
  供簡報與 README 使用；決策層在本機完成的說法以副標呈現，圖中不畫對外模型呼叫。
- README：新增「服務管理與開機自啟」章節——手動 `scripts/agi.sh` 為正式模式（2026-11-25 裁定不實裝 unit），
  另載選用 systemd unit 全文與安裝驗證步驟。
- FastAGI 兩相協議：`AGI(...,start,...)` 登記旁路 session 並立即返回，
  `AGI(...,result,...)` 等 transcript（≤3 秒）、決策、回寫六個 `DBN_*`。
- TTL sweeper：沒走到 `result` 的分支（按 0／掛斷／錄音錯誤／閘滿 stub）在 20 秒後
  close WS 並刪除旁路檔與 `Record` 檔；`out/agi.health.json` heartbeat 供
  `scripts/agi.sh status` 判定 sweeper 存活（含 active session 數與最近 JSONL 時間）。

### Changed

- **文件重構**：README 收斂為唯一部署主線（`git clone` 部署樹＋目錄權限／`.env`／`aliases.json`／
  音檔／dialplan／啟動／冒煙依序成鏈）、`docs/dialplan.md` 只留 dialplan 本體（片段、`DBN_*`、
  按鍵語意、失敗分派）、移除 PoC 驗收清單；人員異動與故障演練移到 README「服務管理與開機自啟」。
  部署以 asterisk 帳號直接執行（免 sudo；`fwconsole` 與 systemd 安裝除外）；開發流程指令以
  `AGENTS.md` §4 為單一來源。
  部署主線改為部署主機單一視角、新增先期條件、目錄／帳號改「建議＋條件」；開發流程獨立成章。
- **STT 改邊錄旁路（Realtime）**：dialplan 只新增三行（`start` AGI、`MixMonitor(,r(...))`、
  無條件 `StopMixMonitor`），`Record(...,4,10,...)` 與 beep／靜音／`#`／按 0 語意不變。
- **401／403 改落總機**（原 PoC 為 `hangup`）：系統故障必須落到人。
  沉默來電（無 commit 且無 partial）仍走 `retry`，不灌總機。
- 通話 wav 在 `result` 或 TTL 到期即刪；JSONL 保留檔名（`eval.py --calls` join key）。
- 文件同步 Phase C：calls.md 收證步驟（wav 即刪下不再拉 `recordings/`）、dialplan.md §3.5
  指向 README、AGENTS/README 狀態標頭（兩相 AGI＋Realtime）、auth 映射維持現狀之註記。

### Removed

- `agi_server.py` 的 `--stt-timeout`／批次 `SttClient` 依賴（離線工具仍用 `handle_turn`）。

### Fixed

- **`normalize()` 的 `PUNCT_RE` 補 ASCII 句點 `.`**：Realtime STT 常輸出
  「呃......徐先生。」「啊......鄭博文。」，`normalize()` 之後殘留「呃......」，
  `_strip_fillers` 剝掉「呃」便停在 `.`，`NOISE_PREFIXES`／稱謂／姓氏前綴全部失效，
  姓名檢索 0 候選（部門偵測是子字串掃描，不受影響）。342 題指標與 20 通劇本不變。

## 2026-11-24（首次公開）

> 當時標記為 `0.1.0`；此後不再推進版本號。

首次公開。這個版本是從內部 PoC 抽取的（來源與範圍見 `docs/provenance.md`），
功能面與 PoC 結案時一致，差別在文件重建與去識別。

### Added

- 中文姓名比對層：NFKC 正規化、OpenCC 簡→繁、漢語拼音（含破音字變體）、模糊音
  （zh/z、n/l、eng/en、ü/u）、六項加權評分、首音節索引桶。
- 決策層：十條固定順序的決策樹、澄清階梯（問部門／請說全名）、alias 政策、
  第二輪 `DBN_CANDS` 收斂、掛線與總機政策。
- FastAGI 單輪處理器（`dialname/agi.py`）與 threaded server（`agi_server.py`，SIGHUP 原子重載）。
- ElevenLabs Scribe v2 STT adapter（`dialname/stt_client.py`）與批次／重播工具（`stt_batch.py`）。
- 正式名冊入口（`dialname/pbx_directory.py`）：FreePBX userman＋`aliases.json` sidecar＋
  `departments.json`，含孤兒檢查（`scripts/check_aliases.py`）。
- 量測工具：`eval.py`（342 題、16 指標、門檻掃描、`--calls` 吃真實通話日誌）、
  單句除錯（`trace.py`）、通話狀態展示（`demo.py`）。
- 劇本回歸工具鏈（`scripts/calls.py`：`table`／`check`／`compare`／`truth-out`／`replay`／`update`）
  與 20 通劇本協議（`docs/calls.md`）。
- 可達面掃描（`scripts/jev_probe.py scan`）：把 `decide()` 的 reason 映射成 L1–L4，
  L4 非空即「是否該重開外部模型」的重開訊號。
- 文件：`docs/design.md`（設計與理由）、`docs/dialplan.md`（部署與手動驗收）、
  `docs/glossary.md`、`docs/io-mapping.md`、`docs/calls.md`、`docs/provenance.md`、`ROADMAP.md`、
  `SECURITY.md`、`AGENTS.md`。
- 授權：AGPL-3.0-or-later。

### Changed

- **決策層零外部模型存取**。原本在「本地收斂不了的多候選」那一段會把候選交給外部分類器；
  可達面掃描顯示該分支在全部現有語料的可達率是 0，委派機制（adapter、payload 契約、
  `--jev-live` 入口）整個移除，行為等價（golden 逐題 hash 未變）。
  理由、證據與重開條件見 `docs/design.md` §4。
- 提示音不再隨 repo 散布：repo 內沒有 `sounds/`，部署位置為
  `/var/lib/asterisk/sounds/dbn/zh/`，清單以 `scripts/check_sounds.py` 的 `EXPECTED` 為準。
- `docs/dialplan.md` 修正音檔部署路徑（原本寫 `/var/lib/asterisk/sounds/zh/`，少一層 `dbn`，
  會導致 `Playback` 全無聲且無明顯報錯）與提示音數量（19）。

### Removed

- 開發日誌（逐日取捨脈絡）、通話劇本的人工紀錄檔與內部計畫：改由 `docs/design.md`＋
  `CHANGELOG.md`＋`docs/calls.md` 承接；逐日脈絡留在維護者的私有 archive。
- 真實通話語料、錄音與 truth.csv：不入公開樹（含個資）。

### Security

- 公開前掃描：API key／密碼／私鑰 0 筆、真實姓名與分機 0 筆、絕對路徑 0 筆。
- `.env`（含 key）與 `data/aliases.json`（含真名）都不入版控；權限 600。
