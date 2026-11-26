# 提示音：台詞、格式與人名音檔

本檔說明 19 支提示音念什麼、何時播，以及人名音檔的格式與命名。repo 不含任何音檔。

**19 支 = 11 支固定台詞＋8 支 `dept-<dept_id>`。** `disclose_names=True` 的姓名音檔不在這 19 支內；
它需要的四支固定句**未實作**（§5）。**檔名與數量以 `scripts/check_sounds.py` 的 `EXPECTED` 為準**；
本檔負責每支的**內容**（念什麼、何時播）。`q-dept-who` 尚未錄製；缺它時
`dept-scoped-ask` 的第 2 段會沒有聲音。

## 1. 固定台詞（11 支）

| 檔名 | 念什麼 | 何時播 |
|---|---|---|
| `welcome` | 「您好，請說出您要找的部門或姓名」 | 每通第一輪錄音前 |
| `processing` | 「請稍候」 | STT 等待期 |
| `q-say-full-name` | **預設的澄清句**：「請說全名」——不確認、不列舉、不洩漏 | 需要澄清、而且不需要問部門時 |
| `q-which-dept` | 「請問是哪個部門？」 | 候選跨部門、來電者沒提部門時 |
| `q-dept-who` | 「請問您要找哪一位？」 | `dept-scoped-ask` 的第 2 段，緊接在 `dept-<dept_id>` 之後（**待錄**） |
| `q-operator` | 「或按零，由專人為您服務」 | **目前未使用**：clarify 輪不提供按 0（`ask` 分支在 `Background` 前就把 `DBN_ATTEMPT` 加到 2 並跳 `ask_rec`，程式待修） |
| `retry` | 「請問您找哪一位？」 | 沒聽清楚、同一輪重問（`retry` 不增加 `DBN_ATTEMPT`） |
| `transferring` | 「為您轉接」 | 預設，不帶名字 |
| `to-operator` | 「正在為您轉接總機」 | 所有轉總機前（`fallback`／`Dial` 失敗／`AGISTATUS` 失敗／`RECORD_STATUS=OPERATOR`／按 0） |
| `not-found` | 「不好意思，無法確認您要找的對象」 | 所有 `fallback` 轉總機前；**服務故障不播** |
| `goodbye` | 「謝謝您的來電」 | 任何掛線前 |

## 2. 部門台詞（8 支）

`dept-<dept_id>`＝部門名（例 `dept-sales`＝「業務部」）。**部門名不算個資**，可以直接播。
`dept_id` 是 ASCII 路由鍵，同時是音檔尾碼與 dialplan label（見 [`glossary.md`](glossary.md)）。

示範名冊的 8 支：`dept-sales`、`dept-cs`、`dept-rd`、`dept-fin`、`dept-wh`、`dept-adm`、
`dept-hr`、`dept-it`。換名冊就換這 8 支，總數仍是 11＋部門數。

## 3. 格式與驗證

- **`.wav`、8 kHz／mono／16-bit**；`Record()` 與 `Playback` 都是窄頻，其他格式會變調或無聲。
- **檔名純 ASCII**：AST 的 `Playback` 對非 ASCII 路徑不可靠，中文只進「念什麼」，不進檔名。
- 開發端先驗格式與齊全度：

  ```bash
  uv run scripts/check_sounds.py <你的音檔目錄>
  ```

  通過時印 `OK: 19 個音檔齊全且格式正確`；非 ASCII、格式不符、少檔都逐條列出並以非零退出碼結束。
  `name-*.wav` 不在 `EXPECTED` 內，檢查工具只會對它印警告。

## 4. 部署位置

部署主機的路徑是 `/var/lib/asterisk/sounds/dbn/zh/`，dialplan 的 `DBN_SOUND` 必須**逐字一致**。
**`dbn/` 少一層＝`Playback` 全無聲、且沒有明顯報錯**——音檔放錯位置不會讓通話失敗，只會安靜。

```bash
# 部署主機：目標目錄要先存在
mkdir -p /var/lib/asterisk/sounds/dbn/zh

# 開發端：上傳（音檔不進 repo，只能檔案傳輸）
scp <你的音檔目錄>/*.wav asterisk@<pbx>:/var/lib/asterisk/sounds/dbn/zh/
```

完整前提與驗收在 [`deploy.md#sounds`](deploy.md#sounds)。

<a id="name-clips"></a>
## 5. 人名音檔：`clip_hash`、`name_clip` 與 manifest

**預設不會用到這一節。** 只有 `disclose_names=True` 的部署才會產生 `name-*` 音檔與
`manifest.json`；預設（`disclose_names=False`）只有 19 支固定／部門音檔。

命名 = **`name-{pid}-{hash8}.wav`**，例：`sounds/zh/name-u001-ce33556e.wav`。

| 部分 | 來源 | 作用 |
|---|---|---|
| `pid` | 名冊的 `Person.pid` | 給人看：log／SIP trace／稽核一眼知道是誰 |
| `hash8` | `decision.clip_hash()` | 失效判斷：姓名或部門一改就換檔 |

**雜湊的完整定義**（`decision` 的常數 `CLIP_HASH_ALGO` / `CLIP_HASH_FIELDS` / `CLIP_HASH_LEN`）：

```
hash8 = sha1( pid + "|" + name + "|" + dept ) 的 hexdigest 前 8 碼
        編碼固定 UTF-8；name／dept 用名冊 JSON 的原字串（繁體、不經拼音或簡化）
```

`u001` / `王偉` / `業務部` → 輸入字串 `u001|王偉|業務部` → `ce33556e`。三種算法得到同一個值
（可用來驗收或手工補檔）：

```bash
printf 'u001|王偉|業務部' | sha1sum | cut -c1-8          # ce33556e
```

```bash
printf 'u001|王偉|業務部' | openssl dgst -sha1 | awk '{print substr($NF,1,8)}'
```

```python
from dialname.core import load_directory
from dialname.decision import clip_hash
clip_hash(load_directory("data/people.json").people[0])   # 'ce33556e'
```

設計取捨（改命名前先看這兩條）：

- **不放姓名、也不放拼音**：`ls sounds/zh/` 若能直接讀出名單，就等於把全公司姓名印出來，
  與列舉風險同一件事（隱私模型見 [`architecture.md#privacy`](architecture.md#privacy)）。
- **不含 voice／版本**：換聲音庫時檔名不變、直接覆蓋；AGI 端不做快取所以不需要失效。
  代價是「重新生成」不能靠檔名區分，要靠 manifest 的 `generated_at`。

### 5.1 `manifest.json`（`decision.name_manifest()`）

`hash` 不可逆，所以建索引時要同時寫 `sounds/zh/manifest.json`：
檔名 → `pid` / `name` / `dept` / `extension` / `text`（實際念出的字串）/ `voice_id` / `generated_at`，
外加 `hash` 區塊（`algorithm` / `input` / `encoding` / `truncate`）讓檔案自己能解釋算法。

| 要回答什麼 | 看 manifest 的哪一欄 |
|---|---|
| 這個檔是誰的 | `clips[<檔名>].name` / `.pid` |
| 誰還沒生成 | `generated_at` 為 `null` |
| 某人的姓名被合成成哪些檔 | 反查 `clips` 中 `pid` 相符者 |
| 演算法是什麼 | 頂層 `hash` |

**manifest 內含姓名 → 敏感資產**：與 wav 同權限、**不進版控**（`.gitignore` 已排除
`sounds/zh/manifest.json` 與 `sounds/zh/name-*.wav`）、不對外 serve。

## 6. `disclose_names=True` 的四支固定句（**未實作**）

設計上要用四支固定句把姓名拼成一句話；這四支**沒有實作**，`agi_server.py` 也**沒有**
`disclose_names` 旗標，所以預設部署不會走到這一條路。

| 檔名 | 念什麼 | 用途 |
|---|---|---|
| `q-is-it` | 「請問是」 | 念名前綴 |
| `q-or` | 「，還是」 | 候選之間 |
| `q-question` | 「？」 | 句尾 |
| `transferring-name` | 「為您轉接」＋人名 | 帶名字的轉接句 |

在這四支與旗標補齊之前，澄清一律用 `q-say-full-name`／`q-which-dept`，轉接一律用
`transferring`（不帶名字）。這是刻意的：不念姓名就不會把電話簿讀給陌生人聽。
