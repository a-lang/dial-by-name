# Changelog

本專案的格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版本號遵循 [Semantic Versioning](https://semver.org/lang/zh-TW/)。

變更行為（決策、門檻、名冊）時必須在這裡記一筆：**改了什麼、為什麼、對指標的影響**。
純內部重構不必逐條列出。

## [Unreleased]

## [0.1.0] — 2026-11-24

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
