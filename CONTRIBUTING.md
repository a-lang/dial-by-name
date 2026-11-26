# 開發指南：環境、測試與回歸

本檔說明開發環境、測試、三層回歸、修改行為的流程與 hook。

## 環境

Linux、**Python 3.11**、[`uv`](https://docs.astral.sh/uv/) 管理虛擬環境；依賴唯一來源是 `uv.lock`，
**不要**用 pip 直接裝，也不要換其他 Python 版本重建（套件行為會不同）。統一使用專案的 `.venv`。

```bash
uv sync                      # 第一次或改過 pyproject.toml 之後
uv sync --reinstall          # .venv 壞掉時重建套件與入口腳本
```

API key：`ELEVENLABS_API_KEY`（userman 模式的 `DBN_MYSQL_*`）放**專案根目錄**的 `.env`，
權限 `600`、不加 `export`、不入版控。離線工具不需要 key；`stt_batch.py` 與 L2 重播才需要。
key 沒設就停下來回報，不要改成假造回應繼續。

<a id="tests"></a>

## 測試

```bash
uv run python -m pytest -q
```

> **本 repo 統一用 `uv run python -m pytest -q`。** 若 `uv run pytest` spawn 失敗，通常是
> `.venv` 的 shebang 指向舊路徑。

## 指標與除錯

```bash
uv run eval.py               # 342 題、17 指標、門檻掃描
uv run trace.py "小明"       # 單句：⑦ clarify → ⑧ decide
uv run demo.py               # 9 個通話狀態展示
```

## 資產檢查

repo 內沒有音檔，也沒有 `data/aliases.json`。音檔檢查對**你的部署目錄或暫存目錄**跑；
`check_aliases.py` 要讀名冊，需要 `.env` 的 `DBN_MYSQL_*`（或 `/etc/freepbx.conf`），
所以在部署主機或設好 DB 的機器上跑：

```bash
# 19 支提示音：ASCII 檔名、8k／mono／16-bit（路徑換成你的部署目錄）
uv run scripts/check_sounds.py /var/lib/asterisk/sounds/dbn/zh

# aliases.json：孤兒／重複／空值／控制字元；exit 0 才可以 reload 服務
uv run scripts/check_aliases.py --source-directory 'PBX Internal Directory'
```

## 三層回歸

| 層 | 跑什麼 | 何時跑 |
|---|---|---|
| L0 | 342 題合成測資（golden） | 每次 pytest |
| L1 | 20 通劇本＋3 個 `l1_only` | 每次 pytest；改決策層後先跑 |
| L2 | 音檔重播（`scripts/calls.py replay`） | 換 STT 模型或驗真實音質時；按需、有 API 成本 |

合成數字**不是準確率**，只是「規則改壞會掉下來」的回歸訊號；警語、指標定義與已知限制見
[`docs/evaluation.md`](docs/evaluation.md)。20 通劇本的撥號協議、判準與執行順序見
[`docs/calls.md`](docs/calls.md)。golden 的更新規則與逐題比對見
[`tests/golden/README.md`](tests/golden/README.md)——**只有刻意改行為時才動**，並在
`CHANGELOG.md` 說明哪一項動了、為什麼。

## 改行為的順序

1. 先判斷是**規則錯**還是**測資錯**：`trace.py` 看單句，`eval.py` 看整體。
2. 改 `dialname/`（產品邏輯在 `core.py`／`decision.py`；入口只能 import `dialname`，
   契約見 [`AGENTS.md`](AGENTS.md) §3）。
3. 跑 `uv run eval.py`，確認每個動到的指標都能解釋。
4. 跑 `uv run python -m pytest -q`，綠了才算完成。
5. 在 [`CHANGELOG.md`](CHANGELOG.md) 記一筆：**改了什麼、為什麼、對指標的影響**。

**不要為了讓測試過而放寬門檻。** 改評分／門檻／決策順序前先讀
[`docs/decision-logic.md`](docs/decision-logic.md)。

## pre-commit

```bash
bash scripts/install-hooks.sh
```

hook 在 staged 含 `dialname/*.py`、`eval.py`、`data/people.json` 時先跑 L0／L1，非零即拒絕
commit。真要跳過就 `--no-verify`，並在 `CHANGELOG.md` **記原因**。

## 文件同步

行為變更要同步三處：`README.md`（對外承諾）、[`docs/decision-logic.md`](docs/decision-logic.md)
（規則與理由）、`CHANGELOG.md`（使用者可見的變更）。文件指令引用的每個路徑／檔案都必須由
更早的步驟產生，不得憑空出現。

## 文件連結

```bash
uv run python scripts/check_docs_links.py
```

掃 `README`／`CONTRIBUTING`／`AGENTS`／`SECURITY`／`ROADMAP` 與 `docs/**/*.md` 的相對連結與
錨點；零網路、不寫檔。**0 broken 才交**——檔名改了、章節搬了都會在這裡現形。
