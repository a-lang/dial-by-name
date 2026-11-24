# 這個公開版的來源

## 一頁摘要

- 這個 repository 是**內部 PoC 的公開抽取版**，不是從零開始的新專案。
- 內部 archive 於 2026-11-24 凍結（PoC 結案快照）；公開樹是**全新的 git 歷史**，
  由該快照的 `git archive` 抽取，不含 `.env`、`out/`、`recordings/`、內部計畫與開發日誌。
- 抽取後做了三件事：**去識別**（真實姓名／分機 → 合成名冊值）、**文件重建**
  （開發日誌的角色拆成 `docs/design.md`＋`CHANGELOG.md`）、**授權**（AGPL-3.0-or-later）。
- 功能面與凍結快照一致：342 題 golden 的指標與逐題 hash 未變，L1 劇本無漂移。

## 留下了什麼、去掉了什麼

| 項目 | 公開樹 | 說明 |
|---|---|---|
| `dialname/`、`agi_server.py`、`eval.py`、`trace.py`、`demo.py`、`stt_batch.py` | 保留 | 功能與行為不變 |
| `data/people.json`、`departments.json`、`call_script.json`、`aliases.example.json` | 保留 | 全部是**合成**資料（刻意安排的同音對） |
| `tests/`（含 `tests/golden/`） | 保留 | golden 是合成測資的指標快照 |
| `docs/glossary.md`、`docs/io-mapping.md`、`docs/dialplan.md` | 保留，區域修正 | dialplan 的音檔部署路徑與數量已修正 |
| `docs/design.md`、`CHANGELOG.md`、`ROADMAP.md`、`SECURITY.md`、`docs/calls.md`、`docs/provenance.md` | **新增** | 開發日誌與內部計畫的內容整理後重生 |
| 開發日誌（逐日取捨脈絡） | **不公開** | 含真實通話紀錄與人名；留在維護者的私有 archive |
| 內部工作計畫（未公開的計畫目錄） | **不公開** | 內容已併入 `docs/design.md`，工作佇列摘要見 `ROADMAP.md` |
| 提示音 `sounds/zh/*.wav`（19 支） | **不公開** | 真人錄音／TTS 的再散布條件另計；規格與部署位置見 `README.md` |
| 真實通話語料、錄音、`truth.csv` | **不公開** | 含個資；真實指標請自行量測 |
| 逐人名音檔（`disclose_names=True` 用） | **不公開** | 從未在預設部署產生 |

## 真實評測的去向

PoC 階段做過真實電話評測（含 userman 名冊的實撥），那些紀錄與原始資料都在私有 archive。
公開樹只保留**合成測資**的回歸指標——它們是「規則壞掉會掉下來」的訊號，不是準確率。
`README.md` 的「量測」章節對這件事有明確警語，請不要引用合成數字當產品效能。

## 授權與第三方

- 本專案以 AGPL-3.0-or-later 釋出（`LICENSE`）。
- 依賴：`pypinyin`（MIT）、`rapidfuzz`（MIT）、`opencc-python-reimplemented`（Apache-2.0）、
  `httpx`（BSD-3-Clause）、`pymysql`（MIT）。各自的授權以套件內公告為準。
- STT 服務（ElevenLabs）與 FreePBX／Asterisk 是外部服務，不在本授權範圍內。

## 如果你要接手

先讀 `AGENTS.md`（契約與地雷）→ `docs/design.md`（為什麼）→ `ROADMAP.md`（下一步）。
改動的驗證門檻是 `uv run pytest -q` 與 `uv run eval.py`；`scripts/install-hooks.sh` 會把
L0／L1 掛進 pre-commit。真實部署請照 `docs/dialplan.md` 的手動驗收清單逐項勾。
