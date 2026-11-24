# 欄位與列舉值字典（Glossary）

每個結構的每個欄位、每個列舉字串代表什麼。**所有列舉值都是從程式碼 grep 出來的，
不是憑印象寫的。**

---

## 1. `Person` — 名冊上的一個人

| 欄位 | 型別 | 意義 | 來源（正式版） |
|---|---|---|---|
| `pid` | str | 唯一鍵。log 對照與音檔命名用的 id（檔名 `name-{pid}-{hash8}.wav`） | `userman_users.id` |
| `name` | str | 顯示名（繁體） | 「全 CJK → `lname+fname`，否則 `fname + " " + lname`」；**不用 displayname**（本站慣例＝分機號） |
| `directory` | str | 來源 directory；必須等於名冊的 `source_directory` | `userman_directories.name` |
| `last_name` | str | 姓（結構化；可空） | `userman_users.lname` |
| `first_name` | str | 名（結構化；可空） | `userman_users.fname` |
| `dept` | str | 目錄原始 `department` 自由文字；載入時解析成 `dept_id`。**空字串合法**；非空但對不到 → 載入錯誤 | `userman_users.department` |
| `title` | str | 職稱，用於消歧（「王經理」） | `userman_users.title` |
| `extension` | str | 轉接目標分機 | `default_extension` |
| `aliases` | str[] | 英文名／綽號／舊名（稀疏：只登記需要小名的人） | userman 沒有 alias 欄位 → sidecar `aliases.json`（key＝`userman_users.id`，不是分機） |

索引時衍生的欄位（不進 JSON，只在記憶體）：

| 衍生欄位 | 意義 |
|---|---|
| `name_key` | `normalize(name)` 後的比對鍵（繁、去標點、ASCII 小寫） |
| `dept_id` | 載入時由 `dept` 解析出的 ASCII 路由鍵；空部門為 `""` |
| `dept_name` | 載入時解析出的正規中文部門名（payload／log 用） |
| `surname_key` | `normalize(last_name)`；沒有欄位時退回 `name_key[:1]`。**不要用字串切推姓**：複姓（歐陽）與西方名（David Wang）都會切錯 |
| `given_key` | `normalize(first_name)`；沒有欄位時退回 `name_key[len(surname_key):]`。用來分辨「只講名字」與「漏字」 |
| `sur_variants` / `given_variants` | 姓與名**各自**的拼音變體（含模糊音版本 `sur_fuzzy` / `given_fuzzy`）。姓同音與名同音的權重不同，必須分開算 |
| `variants` | 所有破音字組合的拼音，例如 曾雅雯 → `[['zeng','ya','wen'], ['ceng','ya','wen']]` |
| `fuzzy_variants` | 上者套用模糊音規則後的版本（`zh→z`、`ing→in`…） |
| `initials` | 聲母序列，例如 `王偉` → `"w w"` |

---

## 2. `Department` — 部門與它的路由鍵

| 欄位 | 意義 | 範例 |
|---|---|---|
| `dept_id` | **唯一 ASCII 鍵**：路由、音檔尾碼 `dept-<id>`、dialplan label `dept_<id>` | `sales` |
| `name` | 正式顯示名（中文）；公告音檔的內容；**不參與路由** | `業務部` |
| `aliases` | 會被剝除／匹配的講法（中英混合） | `["業務","Sales","銷售"]` |

`aliases` 是**部門的講法**，不是人的綽號；人的綽號在 `Person.aliases`。兩者常被搞混。
電話拓樸（queue／ringgroup 號碼）**不在這裡**，由 `docs/dialplan.md` 的 `dept_id → queue` 對映負責。

---

## 3. `Candidate` — 一個候選人

| 欄位 | 意義 |
|---|---|
| `person` | 指向 `Person` |
| `score` | 0–1 的綜合分數（見 §4） |
| `reasons` | 為什麼它被加分，列出字串（見 §6） |

---

## 4. `score` 的語意分級

分數不是「機率」，是**規則式相似度**。實務上的判讀：

| 分數 | 意義 | 系統接下來會做什麼 |
|---|---|---|
| **1.00** | 字面完全命中（含別名命中 0.95） | 若無同音兄弟 → 直接轉 |
| **0.85–0.99** | 高度吻合（同音、少一字） | 通常直接轉；有同音兄弟則澄清 |
| **0.70–0.85** | 像，但可能是同音字選錯 | 幾乎一定澄清 |
| **0.55–0.70** | 只有姓氏或發音接近 | 澄清或問部門 |
| **0.45–0.55** | 勉強算命中（多是單字查詢） | 澄清 |
| **< 0.45** | 視為「沒聽懂」，直接丟棄 | 重問／fallback |
| 單字查詢的特殊上限 | 只講姓 → **cap 0.62**；兩字 vs 三字名 → **cap 0.88** | 保底進澄清 |

分數的組成（`name_score`）：

| 權重 | 成分（`WEIGHTS`） | 說明 |
|---|---|---|
| 0.20 | `char` 漢字逐字相似 | `fuzz.ratio` |
| 0.16 | `char_partial` 漢字部分相似 | `fuzz.partial_ratio` |
| 0.30 | `sur` 姓的拼音（含模糊音 ×0.9） | 姓是封閉集合，同音幾乎同一人 |
| 0.14 | `length` 長度接近度 | 字數差異扣分 |
| 0.10 | `given` 名的拼音（含模糊音 ×0.9） | 名字發散，只能當輔助 |
| 0.10 | `initials` 聲母序相似 | 縮寫／只記得第一個音 |

六項加總 = 1.00（`char` / `char_partial` / `sur` / `length` / `given` / `initials`）。
實作上由 `WEIGHTS` 歸一，**不要在文件或工具裡手寫第二份加總**。

**外加規則**（不屬於上表線性組合）：

- 全名被包含 **≥0.97**、query 被名包含 **0.55 + 0.4×長度比**、英文別名命中 **0.95**
- 部門獨立加減分：同部門 **+0.10**、不同部門 **−0.08**（不是權重項，語意固定）
- 職稱被提及 **+0.04**
- 完全沒有漢字重疊時，拼音只給半權重（`PINYIN_EVIDENCE_NO_CHAR = 0.5`）
- 單字查詢 cap **0.62**、兩字 vs 三字名 cap **0.88**

---

## 5. `Decision` — 一個可執行的決定

| 欄位 | 型別 | 意義 |
|---|---|---|
| `action` | enum | 要做什麼（見 §5.1） |
| `target` | str \| None | 去哪裡：`route` 是分機號；`route_dept` 是 `dept_id`（例 `sales`）；`ask` / `fallback` / `hangup` 為 None |
| `label` | str \| None | 目標名稱（姓名或部門名）。**只給 log 用，不會被播報**（見 README §8 隱私） |
| `reason` | str | 為什麼做這個決定（見 §6） |
| `speech` | str[] | 要依序播放的音檔路徑；空陣列 = 不說話直接轉 |
| `confidence` | float \| None | 採用時的信心值（= 該候選的本地分數） |
| `options` | `[(按鍵, 目的地, 名稱)]` | `ask` 時可按的鍵。**只放系統已知的語意（部門／總機），不放人**。PoC 電話層不消費；按 0 由 `Record(...,o)`（錄音期）與澄清句的 `Background`（播放期）實作，一律轉總機 |

### 5.1 `action` 的五個值

| 值 | PBX 動作 | `target` |
|---|---|---|
| `route` | `Dial(Local/<target>@from-internal)` | 分機號 |
| `route_dept` | 依 `target`（`dept_id`）查 dialplan 的 queue／ringgroup | `dept_id` |
| `ask` | 播放 `speech` → 重新錄音 → 回到正規化 | None |
| `fallback` | 轉總機（已表達找人意圖但收斂不了） | None |
| `hangup` | 播 `speech`（`goodbye`）→ 掛線（無意圖／無回應） | None |

---

## 6. `reason` 全枚舉（決策的「為什麼」）

| 值 | 觸發條件 | 對應 action |
|---|---|---|
| `no-intent` | 第一輪：無候選＋無路由意圖詞＋非「整句只有部門名」＋**未解析出部門**，且殘留不是「姓氏開頭 2–4 字」（2026-11-19 方案 C：部門有解析到改走 `dept-scoped-ask`） | `hangup` |
| `dept-scoped-ask` | 第一輪：無候選＋無路由意圖，但**部門有解析到**（STT 殘骸的實證特徵，如「报街业务部」）；念回部門＋問哪一位，第 2 輪仍無候選 → `no-candidate-giveup` 總機 | `ask` |
| `dept-only` | 只講部門（帶路由意圖詞，或整句只有部門名） | `route_dept` |
| `no-candidate-retry` | 有路由意圖但完全沒候選，第 1 次 | `ask`（重問） |
| `no-candidate-giveup` | 有路由意圖但第 2 次仍沒候選 | `fallback` → 總機 |
| `direct:confident` | 本地判定不需澄清 | `route` |
| `single-candidate` | 只有一個候選（沒有「選」的問題），且分數 ≥ `SINGLE_CANDIDATE_FLOOR`（0.70） | `route` |
| `low-confidence-single` | 只有一個候選，但分數 < 0.70：形狀像陌生人而不是答案（2026-11-17「小米」0.51 → 王小明） | `fallback` → 總機 |
| `surname-only` | 只講姓（含稱謂剝完只剩姓）→ 不追問，直接給總機 | `fallback` |
| `incomplete-name` | 疑似漏字（`張豪` 對 `張家豪`：姓＋名尾字，且本人名 ≥3 字）→ 有 ≥2 候選就不猜 | `fallback` |
| `homophone-sibling-ask` | 本地判定「與同音／近音兄弟撞名」→ **不猜**，直接澄清（跨部門時問部門） | `ask` |
| `alias-ask` | 只講名／小名，alias（同音）命中 **多人** → 一律問部門；第 2 輪仍歧義 → `clarify-exhausted` | `ask` |
| `alias-not-found` | 只講名／小名，發音像名冊裡某人的名，但 **alias 沒登記** → 轉總機（不是掛線） | `fallback` → 總機 |
| `ambiguous-ask:<why>` | (d) 區：低分／拉鋸、候選 ≥2、非同音平手 → 問一句（`<why>` 來自 `needs_clarification()`）；第 2 輪 → `clarify-exhausted` | `ask` |
| `homophone-unresolvable` | 候選 ≥2、**同部門且彼此同音／近音** → 問部門無鑑別力、問全名也敵不過 STT 同音錯誤，直接 fallback（第 1 輪就放棄）。**例外**：`is_given_name_query()` 成立（只講名）→ 改問全名 | `fallback` |
| `clarify-exhausted` | 已問過一輪（`attempt` 2）候選仍 ≥2 → 不再問第三次，也不再轉部門 queue | `fallback` |

---

## 7. `Candidate.reasons` 的標記

| 值 | 意義 |
|---|---|
| `dept:<部門名>` | 與來電者所述部門一致（+0.10） |
| `title:<職稱>` | 查詢字串包含該人職稱（+0.04） |
| `exact-name` | 查詢包含完整姓名（≥0.97） |
| `name-prefix` | 查詢是姓名的前綴（0.55 + 0.4×長度比） |

---

## 8. `needs_clarification()` 的 `why`

| 值 | 意義 | 可調參數 |
|---|---|---|
| `no-candidate` | 沒有任何候選 | — |
| `low-score` | top1 < 0.92 | `confident` |
| `close-race` | top1 − top2 < 0.06 | `margin` |
| `homophone-sibling` | ① 第二名 ≥ 門檻且**與第一名同音/近音**，或 ② **同姓＋名同音不同字**（不看分數） | `HOMOPHONE_FLOOR`（唯一定義在 `core`，**0.70**）／`same_surname_homophone()`。**判斷在 `low-score` 之前**，所以低分查詢也會被檢查 |
| `confident` | 以上皆非 → 可以直接轉 | — |

---

## 9. 音檔命名（`speech` 裡的每個元素）

> **預設不會用到帶雜湊的檔名。** 只有 `disclose_names=True` 的部署才會產生
> `name-*` 音檔與 `manifest.json`；預設（`disclose_names=False`）只有 19 個固定／部門音檔
> （`q-dept-who` 待錄，2026-11-19 加入）：
> `welcome`、`processing`、`retry`、`q-say-full-name`、`q-which-dept`、`q-operator`、
> `transferring`、`to-operator`、`not-found`、`goodbye`、`dept-<dept_id>`、`q-dept-who`。
> **如何啟用**（參數在哪、程式碼怎麼傳、驗收檢查）見 [`README.md` §7.3](../README.md)。

| 路徑 | 內容 |
|---|---|
| `sounds/zh/welcome` | 「您好，請說出您要找的部門或姓名」 |
| `sounds/zh/processing` | 「請稍候」（STT 等待期） |
| `sounds/zh/q-say-full-name` | **預設的澄清句**：「請說全名」——不確認、不列舉、不洩漏 |
| `sounds/zh/q-which-dept` | 「請問是哪個部門？」（候選跨部門、來電者沒提部門時） |
| `sounds/zh/q-dept-who` | 「請問您要找哪一位？」（`dept-scoped-ask` 的第 2 段，緊接在 `dept-<dept_id>` 之後；**待錄**） |
| `sounds/zh/q-operator` | 「或按零，由專人為您服務」（只在 `ask` 後播） |
| `sounds/zh/q-is-it` | 「請問是」（僅 `disclose_names=True`） |
| `sounds/zh/q-or` | 「，還是」（僅 `disclose_names=True`） |
| `sounds/zh/q-question` | 「？」（僅 `disclose_names=True`） |
| `sounds/zh/retry` | 「請問您找哪一位？」 |
| `sounds/zh/transferring` | 「為您轉接」（預設，不帶名字） |
| `sounds/zh/transferring-name` | 「為您轉接」+ 人名（僅 `disclose_names=True`） |
| `sounds/zh/to-operator` | 「正在為您轉接總機」 |
| `sounds/zh/not-found` | 「不好意思，無法確認您要找的對象」（所有 `fallback` 轉總機前；服務故障不播） |
| `sounds/zh/goodbye` | 「謝謝您的來電」（任何掛線前播放） |
| `sounds/zh/dept-<dept_id>` | 部門名（`dept-sales`）；部門名不算個資 |
| `sounds/zh/name-<pid>-<hash8>.wav` | 「姓名，部門」，由 `decision.name_clip()` 產生（僅 `disclose_names=True`；預設部署不生成任何這類檔案） |

### 9.1 人名音檔的命名與雜湊

命名 = **`name-{pid}-{hash8}.wav`**（僅 `disclose_names=True`），
例：`sounds/zh/name-u001-ce33556e.wav`。

| 部分 | 來源 | 作用 |
|---|---|---|
| `pid` | 名冊的 `Person.pid` | 給人看：log／SIP trace／稽核一眼知道是誰 |
| `hash8` | `decision.clip_hash()` | 失效判斷：姓名或部門一改就換檔 |

**雜湊的完整定義**（`decision` 的常數：`CLIP_HASH_ALGO` / `CLIP_HASH_FIELDS` / `CLIP_HASH_LEN`）：

```
hash8 = sha1( pid + "|" + name + "|" + dept ) 的 hexdigest 前 8 碼
        編碼固定 UTF-8；name／dept 用名冊 JSON 的原字串（繁體、不經拼音或簡化）
```

`u001` / `王偉` / `業務部` → 輸入字串 `u001|王偉|業務部` → `ce33556e`。

三種算法必須得到同一個值（可用來驗收或手工補檔）：

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

- **不放姓名、也不放拼音**：`ls sounds/zh/` 若能直接讀出名單，就等於把全公司姓名
  印出來，與 §8 的列舉風險同一件事。
- **不含 voice／版本**：換聲音庫時檔名不變、直接覆蓋，AGI 端不做快取所以不需要失效；
  代價是「重生成」不能靠改檔名區分，要靠 manifest 的 `generated_at`。

### 9.2 `manifest.json`（`decision.name_manifest()`）

`hash` 不可逆，所以建索引時要同時寫 `sounds/zh/manifest.json`：
檔名 → `pid` / `name` / `dept` / `extension` / `text`（實際念出的字串）/ `voice_id` / `generated_at`，
外加 `hash` 區塊（`algorithm` / `input` / `encoding` / `truncate`）讓檔案自己能解釋算法。

| 要回答什麼 | 看 manifest 的哪一欄 |
|---|---|
| 這個檔是誰的 | `clips[<檔名>].name` / `.pid` |
| 誰還沒生成 | `generated_at` 為 `null` |
| 某人的姓名被合成成哪些檔 | 反查 `clips` 中 `pid` 相符者 |
| 演算法是什麼 | 頂層 `hash` |

**manifest 內含姓名 → 敏感資產**：與 wav 同權限、**不進版控**（`.gitignore` 已排除 `sounds/zh/manifest.json` 與 `sounds/zh/name-*.wav`）、不對外 serve。

---

## 10. `eval.py` 的 `kind`（測資類型）

**除 `english` 外，全部以簡體送出**（模擬真實 STT 的輸出字形，經 OpenCC `t2s` 轉換）。
`-sc` 後綴就是這個意思。

| `kind` | 測什麼 |
|---|---|
| `exact-sc` | 簡體全名（**主要情境**） |
| `exact-tc` | 繁體全名（對照組：少數引擎輸出繁體／人工輸入） |
| `homophone-sc` | 同音字替換 + 簡體（最真實的錯誤組合） |
| `homophone-dept-sc` | 同音字替換 + **報了部門**（部門是封閉詞彙，應該足以解掉同音歧義 → top-1 必須對） |
| `drop-char-sc` | 漏中間一個字 + 簡體 |
| `surname-only-sc` | 只講姓 + 稱謂 |
| `given-name-only-sc` | 只講名字 |
| `dept-only-sc` | 只講部門 |
| `dept+surname-sc` | 部門 + 姓 + 稱謂 |
| `english` | 英文別名（ASCII，不受簡繁影響） |
| `filler-sc` | 前後加贅詞（「麻烦」是簡體，用來測 S2T） |
| `garbage` | **完全不是要找人的句子**（訂便當、問地址、談業務合作…）→ 正確行為是「零候選」 |
| `nickname-trap` | 日常稱謂（王伯伯／陳董）→ 命中同姓算可接受，但**必須進澄清** |

---

## 11. `eval.py` 的輸出（四份東西）

`eval.py` 產出「一份檔案 + 三段 stdout」。四者刻意分開：檔案給程式比對，stdout 給人看。

```
out/eval_rows.json    342 筆逐列結果（12 欄）
out/eval_summary.json n + 11 個彙總指標
stdout ①              cases / 11 指標（= summary 的內容）
stdout ②              門檻掃描表（5 個 floor）
stdout ③              失敗案例清單（top3 全落空的）
```

`out/` 已在 `.gitignore`（每次跑都變）。要留證據就貼進 issue／PR，不要進版控。

### 11.1 `out/eval_rows.json`：12 欄

| # | 欄位 | 型別 | 怎麼算 | 沒有值的時候 |
|---|---|---|---|---|
| 1 | `query` | str | 實際送進 `Directory.search()` 的字串（簡體） | — |
| 2 | `kind` | str | 測資類型（13 種，見 §10） | — |
| 3 | `top1` | str \| null | 第一名候選的 `pid` | 無候選 → `null` |
| 4 | `top1_name` | str \| null | 該人的顯示名（繁） | 無候選 → `null` |
| 5 | `top1_score` | float | 該人的分數 | 無候選 → `0.0` |
| 6 | `margin` | float | `top1 − top2`（前二名差距） | 候選 < 2 → `1.0`（不是 0） |
| 7 | `hit1` | bool | `top1 ∈ expect` | `expect` 為空（dept-only）→ `false` |
| 8 | `hit3` | bool | 前三名任一 `∈ expect` | 同上 → `false` |
| 9 | `dept_ok` | bool \| null | 偵測到的部門 == `dept_expect` | 只有 `dept-only-sc` 有值，其餘 `null` |
| 10 | `flagged` | bool \| null | `needs_clarification()` 的結果（要不要追問） | `dept-only-sc` → `null`（沒有澄清問題） |
| 11 | `why` | str | 為什麼要／不要澄清（§8）或 `dept-only` | — |
| 12 | `hit1_or_any` | bool | 「有沒有抓到任何一個人」（`top1 != null`） | 只用來判 `garbage`：亂句不該有候選 |

**`expect` 是「可接受的 pid 集合」**，不是單一答案：同音題（`homophone-sc`）允許落在同音群裡的任何一個。

四種代表性列（實際輸出）：

```json
{ "query": "王伟",     "kind": "exact-sc",      "top1": "u001", "top1_score": 1.0,
  "margin": 0.153, "hit1": true,  "hit3": true,  "dept_ok": null,
  "flagged": true,  "why": "homophone-sibling" }

{ "query": "汪伟",     "kind": "homophone-sc",  "top1": "u002", "top1_score": 1.0,
  "margin": 0.153, "hit1": true,  "hit3": true,  "dept_ok": null,
  "flagged": true,  "why": "homophone-sibling" }

{ "query": "王先生",   "kind": "surname-only-sc","top1": "u001", "top1_score": 0.62,
  "margin": 0.0,   "hit1": true,  "hit3": true,  "dept_ok": null,
  "flagged": true,  "why": "low-score" }

{ "query": "帮我接业务", "kind": "dept-only-sc",  "top1": null,   "top1_score": 0.0,
  "margin": 1.0,   "hit1": false, "hit3": false, "dept_ok": true,
  "flagged": null, "why": "dept-only" }
```

判讀兩個眉角：

- **`margin: 0.0` + `hit1: true`** 是正常的（「王先生」兩個王同分，`expect` 含兩個）。
- **`flagged: true` 配 `hit1: true`** 也是正常的（字面命中，但名冊裡有同音兄弟 → 還是要問）。

### 11.2 `out/eval_summary.json`：`n` + 16 個指標

| 指標 | 計算式 | 目前值 | 判讀方向 |
|---|---|---|---|
| `n` | 測資總數 | **342** | — |
| `silent_wrong_rate` | 沒被標記澄清 **且** `hit1` 為 false 的比例（排除 `dept-only-sc`） | **0.000** | **最重要**：這是「安靜地轉錯人」的機率，必須趨近 0 |
| `top1_easy_sc` | `{exact-sc, drop-char-sc, english, filler-sc}` 的 `hit1` 平均 | **1.000** | 越高越好；掉下來代表比對層退步 |
| `top1_easy_tc` | `{exact-tc}` 的 `hit1` 平均 | **1.000** | 繁體對照組，應與 `top1_easy_sc` 一致 |
| `sc_vs_tc_gap` | `exact-sc` 的 `hit1` − `exact-tc` 的 `hit1` | **0.000** | ≠ 0 代表簡繁正規化出問題 |
| `recall3_hard` | `HARD_KINDS`（同音／只講姓／只講名／部門+姓）的 `hit3` | **1.000** | 疑難題「正確的人在不在前三名」 |
| `top1_homophone` | `{homophone-sc}` 的 `hit1` | **0.778** | **不該追求 1.0**：同音題本來就該靠澄清，這裡只確認沒有系統性偏好 |
| `homophone_dept_top1` | `{homophone-dept-sc}` 的 `hit1` | **1.000** | 同音＋報部門時 top-1 應該正確；掉下來代表「部門線索沒被用來解歧義」（`DEPT_SCOPE_MIN` 或部門加權壞了） |
| `dept_acc` | `{dept-only-sc}` 的 `dept_ok` | **1.000** | 只講部門時有沒有轉對 |
| `clarify_rate_hard` | `HARD_KINDS` 被標記澄清的比例 | **0.993** | 越接近 1 越安全（該問的都有問） |
| `clarify_rate_easy` | `STRICT_KINDS`（`exact-sc, exact-tc, english, filler-sc`）被標記澄清的比例 | **0.230** | 使用者體驗成本；**多數是名冊裡真的有同音兄弟，不是誤報** |
| `garbage_false_match` | `{garbage}` 中 `hit1_or_any` 為 true 的比例 | **0.000** | **亂句硬抓到人** —— 比同音字選錯嚴重，必須 0 |
| `nickname_trap_clarify` | `{nickname-trap}` 被標記澄清的比例 | **1.000** | 「王伯伯」這種稱謂要問，不能直接轉 |
| `no_requery_rate` | 跑決策層後，`{surname-only-sc, nickname-trap, drop-char-sc}` 中 `action != ask` 的比例 | **1.000** | 只講姓／只有稱謂／漏字時**不該再追問**（走 `surname-only` / `incomplete-name` fallback 或直接轉） |
| `third_ask_rate` | 第 1 輪追問的題目，在第 2 輪仍為 `ask` 的比例 | **0.000** | 兩輪為上限：第 2 輪仍歧義就 `clarify-exhausted` → fallback |
| `garbage_false_route` | 跑決策層後，`{garbage}` 中 `action ∈ {route, route_dept}` 的比例 | **0.000** | **亂句被轉出去**（轉人或轉部門都算）—— 靠 `has_routing_cue()` 閘門擋住 |
| `dept_route_recall` | `{dept-only-sc}` 中真的有轉到部門的比例 | **1.000** | 確認閘門沒把正常流量也擋掉 |

`HARD_KINDS` / `STRICT_KINDS` 的定義在 `eval.py` 頂端（`HARD_KINDS`、`STRICT_KINDS` 常數），
`drop-char-sc` 刻意**只算在 `EASY_KINDS`、不算在 `STRICT_KINDS`** —— 少一個字本來就有歧義。

**判讀順序**：先看 `silent_wrong_rate`（安全），再看 `recall3_hard`（能力），
最後才看 `clarify_rate_easy`（體驗）。前者不合格時，後者再漂亮都沒有意義。

### 11.3 門檻掃描（stdout ②）

固定掃 `homophone_floor ∈ {0.45, 0.55, 0.65, 0.75, 0.85}`，每列三欄：

| 欄位 | 意義 |
|---|---|
| `silent_wrong` | 同 `silent_wrong_rate`（全體） |
| `clarify_hard` | 疑難題被追問的比例（越高越安全） |
| `clarify_easy` | 無歧義題被追問的比例（越低體驗越好） |

目前（342 筆）：

```
  floor | silent_wrong | clarify_hard | clarify_easy
   0.45 |        0.000 |        0.993 | 0.230
   0.55 |        0.000 |        0.993 | 0.230
   0.65 |        0.000 |        0.993 | 0.230
   0.75 |        0.000 |        0.993 | 0.230
   0.85 |        0.012 |        0.949 | 0.097
```

**選法**：找 `silent_wrong` 還維持 0 的最小 `clarify_easy` → 落在 **0.65–0.75**。
`decide()` 實用值是 **0.70**，定義在 `core.HOMOPHONE_FLOOR`（`needs_clarification()` 與
`decide()` 共用同一個常數，沒有第二份數字）。

### 11.4 失敗案例清單（stdout ③）

條件：`kind ∈ HARD_KINDS ∪ {exact-sc, exact-tc}` **且** `hit3 == false`。
輸出格式：

```
  [kind] 'query' -> top1_name (top1_score)
```

**印不出東西是好事**（目前就是空的）—— 代表疑難題與全名題的前三名都沒有落空。
一旦有內容，就是最該先看的 regression：它比 `top1` 掉分更嚴重（正確答案連前三名都進不去）。

---

## 12. 常數一覽（要調參時從這張表下手）

| 常數 | 值 | 位置 | 作用 |
|---|---|---|---|
| `WEIGHTS` | char 0.20 / partial 0.16 / **sur 0.30** / **length 0.14** / **given 0.10** / initials 0.10 | core | 六項加權唯一定義（總和 1.0）。優先序：姓的同音 > 字數 > 名的同音；`length` 只在 `char_partial > 0` 時計分 |
| `PINYIN_EVIDENCE_NO_CHAR` | 0.5 | core | 完全沒有漢字重疊時，`sur`/`given` 只給半權重，避免亂句靠拼音相似混過門檻 |
| `LENGTH_SHRINK_POWER` | 2.0 | core | 查詢比姓名多字時的結構折扣指數：`(len_name/len_query)**2` |
| `MIN_SCORE` | 0.45 | core | 低於此視為沒聽懂（單字查詢放寬 0.30） |
| 單字 cap | 0.62 | core | 只講姓不可能唯一 |
| 兩字 cap | 0.88 | core | 兩字查詢 vs 三字名 |
| `margin` | 0.06 | core | 前二名差距多大算拉鋸 |
| `confident` | 0.92 | core | 低於此就要澄清 |
| `HOMOPHONE_FLOOR` | **0.70** | core（唯一定義） | 第二名多高才觸發同音檢查；`needs_clarification()` 與 `decide()` 共用 |
| `CONFIDENT_SCORE` | 0.92 | core | 第一名要到此分數才可能跳過澄清 |
| `DEPT_BONUS` / `DEPT_PENALTY` | +0.10 / −0.08 | core | **部門加權**：同部門 +0.10、不同 −0.08（消融：關掉後 `homophone_dept_top1` 0.444） |
| `TITLE_BONUS` | 0.04 | core | 職稱相符加分 |
| `DEPT_SCOPE_MIN` | **1** | core | 部門收斂候選池的最小人數；1＝部門內唯一的人也收斂（部門是封閉詞彙，可信） |
| `band` | 0.80 | decision | 澄清句只列 ≥ top×0.8 的候選 |
| `OPERATOR_DEST` | `operator` | decision | 只給 `Decision.options`／log；實際總機落點在 dialplan `DBN_OPERATOR` |
| `attempt` | 1→2 | decision | 最多問兩輪；第 2 輪仍歧義 → `clarify-exhausted` → **fallback**（不轉部門 queue） |
| `ROUTING_CUES` | 找／接／轉／分機／窗口／聯絡／聯繫／打給 | core | 部門路由的意圖閘門：沒有這些詞就只接受「整句只有部門名」 |
| `dept_residual` | `search()` 的第三個回傳值 | decision | 傳 `""` 且 `dept` 存在＝整句只有部門名；**呼叫端必須傳**，漏傳會讓「只講部門」被 `no-intent` 誤掛線 |
