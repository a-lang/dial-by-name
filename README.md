# dial-by-name

**「您好，請說出您要找的部門或姓名。」**

[FreePBX](https://osslab.tw/books/asterisk/chapter/freepbx)／[Asterisk](https://osslab.tw/books/asterisk) 的中文「說姓名／部門 → 轉分機」層：在本機用拼音比對收斂候選、必要時只追問一次，
再把決策交給 dialplan 轉到正確分機。**電話簿不出機器**，也不播報姓名或號碼。

![封面：四層（電話層／AGI 服務／決策核心／名冊）× 五階段（收音・轉錄・收斂・決策・執行）流程](assets/cover.png)

## 適用與不適用

| 情境 | 支援程度 |
|---|---|
| FreePBX 上「講中文名字／部門 → 分機」 | ✅ 就是這個 |
| clone 完就能用 | ❌ 參考實作；userman／dialplan／19 支音檔要自己接 |
| 英文姓名、多語系 | ❌ 目前只做中文 |
| 語音完全不外送 | ⚠️ 只有語音轉文字（STT）外送 ElevenLabs；決策 100% 本地 |
| 高併發 | ❌ 並發閘 4、session TTL 20 秒 |

## 通話流程

```
1. dialplan 播 welcome、Record() 收音；AGI start 登記 session 後立即返回
2. dialplan 播 processing，AGI result 收 WAV tail
3. AGI 把 WAV 送 ElevenLabs Realtime STT → 逐字稿（唯一外送）
4. core 在本機做正規化、拼音、模糊音 → 候選收斂（top-k，預設 20；毫秒級）
5. decide() 給出 route／route_dept／ask／hangup
6. dialplan 依 DBN_* 轉接、播下一句或送總機
```

- **AGI（Asterisk Gateway Interface，Asterisk 閘道介面）只做 STT＋決策**：收音、播音、轉接、回合計數全在 dialplan——跨輪狀態只有一處，
  AGI 壞掉時 dialplan 仍有 `AGISTATUS` 安全網。
- **決策層零外部呼叫**：`decide()` 沒有委派參數；500 人的候選清單不進模型，
  同音靠發音、字串比對 3 ms 就做完。
- **安全拒猜**：同音兄弟跨部門先問部門、同部門 fallback，第二輪仍拉鋸交總機。

## 快速開始

在 PBX 上以 `asterisk` 帳號操作；完整指令與失敗排查在 [`docs/deploy.md`](docs/deploy.md)。

1. **取得程式碼**（約 1 分鐘）— `git clone <repo-url> /var/lib/asterisk/dial-by-name`
   預期：`ls pyproject.toml uv.lock` 兩個檔名都在。
2. **建 `.venv`**（約 2 分鐘）— `uv sync`
   預期：`.venv/bin/python -V` → `Python 3.11.x`。
3. **建可寫目錄**（10 秒）— `mkdir -p recordings out/calls && chmod -R 0770 recordings out`
   預期：`touch recordings/.probe out/calls/.probe` 不報錯。
4. **寫 `.env`**（約 1 分鐘）— 照 [`deploy.md#env`](docs/deploy.md#env) 的範本；金鑰不進指令行。
   預期：`ls -l .env` 權限 `-rw-------`。
5. **放 19 支提示音**（約 5 分鐘）— PBX 上 `mkdir -p /var/lib/asterisk/sounds/dbn/zh`；
   開發端 `uv run scripts/check_sounds.py <目錄>`，再
   `scp <目錄>/*.wav asterisk@<pbx>:/var/lib/asterisk/sounds/dbn/zh/`。
   預期：`OK: 19 個音檔齊全且格式正確`；`ls …/dbn/zh/*.wav | wc -l` → `19`。
6. **啟動看金絲雀**（10 秒）— `scripts/agi.sh start`；`tail -n 5 out/agi.log`
   預期：`started pid <pid>  listen 127.0.0.1:4573` 與
   `roster: source='…' people=N aliases=M departments=D`。

userman 模式的 `data/aliases.json`、dialplan 貼上與冒煙撥號是 [`deploy.md`](docs/deploy.md) 的第 5、7、9 步。

## 系統需求

| 需求 | 說明 |
|---|---|
| FreePBX／Asterisk 22+ | dialplan 貼 `extensions_custom.conf`；名冊走 FreePBX userman |
| `uv`＋Python 3.11 | 部署樹的 `.venv` 由 `uv sync` 產生 |
| 對外 443（WSS 到 `api.elevenlabs.io`） | 唯一外部依賴；連不出去＝每通 `fallback` → 總機（不是掛線） |
| `ELEVENLABS_API_KEY` | 只寫部署樹的 `.env`：權限 600、不入版控 |
| DB 唯讀存取 | `/etc/freepbx.conf` 可讀，或 `.env` 的 `DBN_MYSQL_*` 四鍵；兩邊都不成＝拒絕啟動 |
| `recordings/` 可寫 | `Record()`／`MixMonitor()` 不會自建目錄；不存在＝錄音失敗 |

## 量測結果

| 指標 | 數值 |
|---|---|
| 誤轉率（沒澄清且答錯，`silent_wrong`） | 0.000 |
| 疑難查詢 recall@3（同音、只講姓、只講名、部門+姓） | 1.000 |
| 同音＋報了部門 top-1（`homophone_dept_top1`） | 1.000 |

> **342 題是合成測資**：對照表模擬 STT 的同音錯誤，不是真實錄音，**不是準確率**。
> 它只有一個用途——規則改壞時會掉下來。指標定義、消融與效能見 [`docs/evaluation.md`](docs/evaluation.md)。

## 已知限制

- **真實 STT 錯誤率未量測**：8 kHz 窄頻＋台灣國語＋姓名的 WER／CER 要靠你自己的錄音。
- **keyterms 未啟用**：ElevenLabs keyterms 的 A/B 還沒做（模型吐簡體，繁體 keyterm 可能白費）。
- **並發閘 4（預設；生產不暴露）**：四個沒走到 `result` 的 session 在 20 秒 TTL 內佔滿閘，期間新話務全落總機。
- **TTL 20 秒＝隱私窗口**：殘留 wav 由 sweeper 在 20 秒內刪；`kill -9` 後沒有外部清理器。
- **`disclose_names=True` 未完整**：念姓名所需四支固定句與 `agi_server.py` 旗標未實作。

完整清單與失敗觀測面見 [`docs/evaluation.md#limits`](docs/evaluation.md#limits)。

## 文件導覽

- **部署**：先期條件與 10 步主線 [`docs/deploy.md`](docs/deploy.md) →
  dialplan 片段 [`docs/dialplan.md#snippet`](docs/dialplan.md#snippet) → 19 支台詞 [`docs/sounds.md`](docs/sounds.md)。
- **維運**：啟停與 health [`docs/operations.md`](docs/operations.md) →
  人員異動 [`operations.md#roster`](docs/operations.md#roster) → 20 通劇本 [`docs/calls.md`](docs/calls.md)。
- **理解與改規則**：架構與隱私 [`docs/architecture.md`](docs/architecture.md) →
  決策順序與門檻 [`docs/decision-logic.md`](docs/decision-logic.md) →
  AGI 契約 [`docs/agi-protocol.md`](docs/agi-protocol.md)；開發環境與回歸紀律見
  [`CONTRIBUTING.md`](CONTRIBUTING.md)。
- **查名詞**：欄位、列舉、常數與縮寫 [`docs/glossary.md`](docs/glossary.md#abbreviations)。

## 授權與來源

AGPL-3.0-or-later（見 [`LICENSE`](LICENSE)）。本 repo 是內部 PoC 的公開抽取版，
名冊與測資都是合成資料。第三方依賴：`pypinyin`／`rapidfuzz`（MIT）、
`opencc-python-reimplemented`（Apache-2.0）、`httpx`（BSD-3-Clause）、
`pymysql`（MIT）。安全回報見 [`SECURITY.md`](SECURITY.md)，變更紀錄在
[`CHANGELOG.md`](CHANGELOG.md)，下一步在 [`ROADMAP.md`](ROADMAP.md)。
