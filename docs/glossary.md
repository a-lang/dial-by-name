# 欄位、列舉、常數與縮寫

本檔定義型別、欄位、列舉值、常數與縮寫。

每個結構的每個欄位、每個列舉字串代表什麼。**列舉值以程式碼為準，不要憑印象加值。**

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

## 2. `Department` — 部門與它的路由鍵

| 欄位 | 意義 | 範例 |
|---|---|---|
| `dept_id` | **唯一 ASCII 鍵**：路由、音檔尾碼 `dept-<id>`、dialplan label `dept_<id>` | `sales` |
| `name` | 正式顯示名（中文）；公告音檔的內容；**不參與路由** | `業務部` |
| `aliases` | 會被剝除／匹配的講法（中英混合） | `["業務","Sales","銷售"]` |

`aliases` 是**部門的講法**，不是人的綽號；人的綽號在 `Person.aliases`。兩者常被搞混。
電話拓樸（queue／ringgroup 號碼）**不在這裡**，由 [`dialplan.md`](dialplan.md) 的
`dept_id → queue` 對映負責。

## 3. `Candidate` — 一個候選人

| 欄位 | 意義 |
|---|---|
| `person` | 指向 `Person` |
| `score` | 0–1 的綜合分數（見 §4） |
| `reasons` | 為什麼它被加分，列出字串（見 §7） |

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

## 5. `Decision` — 一個可執行的決定

| 欄位 | 型別 | 意義 |
|---|---|---|
| `action` | enum | 要做什麼（見 §5.1） |
| `target` | str \| None | 去哪裡：`route` 是分機號；`route_dept` 是 `dept_id`（例 `sales`）；`ask` / `fallback` / `hangup` 為 None |
| `label` | str \| None | 目標名稱（姓名或部門名）。**只給 log 用，不會被播報**（見 [`architecture.md#privacy`](architecture.md#privacy)） |
| `reason` | str | 為什麼做這個決定（見 §6） |
| `speech` | str[] | 要依序播放的音檔路徑；空陣列 = 不說話直接轉 |
| `confidence` | float \| None | 採用時的信心值（= 該候選的本地分數） |
| `options` | `[(按鍵, 目的地, 名稱)]` | `ask` 時可按的鍵。**只放系統已知的語意（部門／總機），不放人**。電話層不消費；按 0 由 `Record(...,o)`（錄音期）實作，轉總機；clarify 輪不提供按 0（`q-operator` 目前未使用，見 [`sounds.md`](sounds.md)） |

### 5.1 `action` 的五個值

| 值 | PBX 動作 | `target` |
|---|---|---|
| `route` | `Dial(Local/<target>@from-internal)` | 分機號 |
| `route_dept` | 依 `target`（`dept_id`）查 dialplan 的 queue／ringgroup | `dept_id` |
| `ask` | 播放 `speech` → 重新錄音 → 回到正規化 | None |
| `fallback` | 轉總機（已表達找人意圖但收斂不了） | None |
| `hangup` | 播 `speech`（`goodbye`）→ 掛線（無意圖／無回應） | None |

（`DBN_ACTION` 另有第六個傳輸層值 `retry`，不在 `Decision.action` 裡，見
[`agi-protocol.md`](agi-protocol.md) §8。）

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
| `alias-ask` | 只講名／小名，alias（同音）命中 **多人** → 預設問部門；候選全在同一個部門（或已提到部門）時改問全名。第 2 輪仍歧義 → `clarify-exhausted` | `ask` |
| `alias-not-found` | 只講名／小名，發音像名冊裡某人的名，但 **alias 沒登記** → 轉總機（不是掛線） | `fallback` → 總機 |
| `ambiguous-ask:<why>` | (d) 區：低分／拉鋸、候選 ≥2、非同音平手 → 問一句（`<why>` 來自 `needs_clarification()`）；第 2 輪 → `clarify-exhausted` | `ask` |
| `homophone-unresolvable` | 候選 ≥2、**同部門且彼此同音／近音** → 問部門無鑑別力、問全名也敵不過 STT 同音錯誤，直接 fallback（第 1 輪就放棄）。**例外**：`is_given_name_query()` 成立（只講名）→ 改問全名 | `fallback` |
| `clarify-exhausted` | 已問過一輪（`attempt` 2）候選仍 ≥2 → 不再問第三次，也不再轉部門 queue | `fallback` |

## 7. `Candidate.reasons` 的標記

| 值 | 意義 |
|---|---|
| `dept:<部門名>` | 與來電者所述部門一致（+0.10） |
| `title:<職稱>` | 查詢字串包含該人職稱（+0.04） |
| `exact-name` | 查詢包含完整姓名（≥0.97） |
| `name-prefix` | 查詢是姓名的前綴（0.55 + 0.4×長度比） |
| `alias` | 只講名／小名命中 sidecar 的 alias 白名單（分數上限固定 0.95，門檻歸零） |

## 8. `needs_clarification()` 的 `why`

| 值 | 意義 | 可調參數 |
|---|---|---|
| `no-candidate` | 沒有任何候選 | — |
| `low-score` | top1 < 0.92 | `confident` |
| `close-race` | top1 − top2 < 0.06 | `margin` |
| `homophone-sibling` | ① 第二名 ≥ 門檻且**與第一名同音/近音**，或 ② **同姓＋名同音不同字**（不看分數） | `HOMOPHONE_FLOOR`（唯一定義在 `core`，**0.70**）／`same_surname_homophone()`。**判斷在 `low-score` 之前**，所以低分查詢也會被檢查 |
| `confident` | 以上皆非 → 可以直接轉 | — |

## 9. 音檔命名

19 支台詞（念什麼、何時播）、8 kHz／mono／16-bit 格式、ASCII 檔名，以及
`disclose_names=True` 的 `name-<pid>-<hash8>.wav`／`manifest.json` 命名與 hash，見
[`sounds.md`](sounds.md)。

## 10. 測資 `kind`

`eval.py` 的測資類型（`exact-sc`／`homophone-dept-sc`／`garbage`…；目前 13 種實際出現，
名冊有對應 alias 時多一種 `alias-given`）見
[`evaluation.md`](evaluation.md)。

## 11. `eval.py` 的輸出

輸出（`out/eval_rows.json` 的 12 欄、`out/eval_summary.json` 的指標、門檻掃描、失敗清單）見
[`evaluation.md`](evaluation.md)。

<a id="constants"></a>
## 12. 常數一覽

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

<a id="abbreviations"></a>
## 13. 縮寫與產品名

實際出現的縮寫與專有名詞；高頻者（STT、AGI）在第一次出現時展開，其餘查這裡。

| 縮寫 | 全稱 | 中文 | 一句說明 |
|---|---|---|---|
| AGI | Asterisk Gateway Interface | Asterisk 閘道介面 | Asterisk 呼叫外部服務的協議；本專案的 FastAGI 服務 |
| AGPL | GNU Affero General Public License | GNU Affero 通用公共授權 | 本專案的授權條款 |
| API | Application Programming Interface | 應用程式介面 | 例如 ElevenLabs API |
| ASCII | American Standard Code for Information Interchange | 美國資訊交換標準碼 | 檔名與通道變數只允許 ASCII |
| CER | Character Error Rate | 字元錯誤率 | 以字元計算的 STT 錯誤率；`stt_batch.py` 用這個 |
| CI | Continuous Integration | 持續整合 | 本 repo 沒有 CI，以 pre-commit 代替 |
| CLI | Command-Line Interface | 命令列介面 | `eval.py`／`trace.py` 等 |
| CSV | Comma-Separated Values | 逗號分隔值 | `truth.csv` |
| DB | Database | 資料庫 | FreePBX 的 MySQL／MariaDB；本專案唯讀 |
| DTMF | Dual-Tone Multi-Frequency | 電話按鍵音 | 按 0 轉總機 |
| EOF | End of File | 檔案／串流結束 | WAV tail 與 WS 收尾 |
| GUI | Graphical User Interface | 圖形介面 | FreePBX Admin |
| HTTP | Hypertext Transfer Protocol | 超文本傳輸協定 | WS 握手與 API |
| ISO | International Organization for Standardization | 國際標準化組織 | ISO 8601 時間格式 |
| IVR | Interactive Voice Response | 互動式語音應答 | 本專案的整體形態 |
| JSON | JavaScript Object Notation | JavaScript 物件表示法 | 結構化資料格式 |
| JSONL | JSON Lines | 一行一筆 JSON | `out/calls/*.jsonl` |
| LDAP | Lightweight Directory Access Protocol | 輕量目錄存取協定 | 企業目錄整合；本專案只讀 userman 名冊 |
| MIT | MIT License | MIT 授權 | 部分依賴的授權 |
| NFKC | Normalization Form KC | Unicode 相容正規化 | `normalize()` 的第一步 |
| PBX | Private Branch Exchange | 企業電話交換機 | FreePBX／Asterisk 提供的分機系統 |
| PCM | Pulse-Code Modulation | 脈衝編碼調變 | Realtime STT 的線性音訊串流 |
| PID | Process ID | 行程識別碼 | `out/agi.pid` |
| SIGHUP | Signal Hangup | 掛斷訊號 | 本專案用來原子重載名冊 |
| SSH | Secure Shell | 安全遠端登入 | 登入 PBX |
| STT | Speech-to-Text | 語音轉文字 | 本專案唯一的第三方服務（ElevenLabs） |
| TTL | Time To Live | 存活時間 | session 20 秒清理窗口 |
| TTS | Text-to-Speech | 文字轉語音 | 離線產生提示音；不在通話路徑 |
| VAD | Voice Activity Detection | 語音活動偵測 | 分辨沉默與有說話 |
| WAV | Waveform Audio File Format | 波形音訊檔格式 | `Record()` 的錄音；WAV tail 送 STT |
| WER | Word Error Rate | 詞錯誤率 | 以詞計算的 STT 錯誤率；中文斷詞有歧義 |
| WS | WebSocket | 全雙工連線 | Realtime STT 傳輸 |
| WSS | WebSocket Secure | 加密 WebSocket | 對外 443 到 ElevenLabs |

產品與專有名詞：

| 名稱 | 全稱 | 說明 |
|---|---|---|
| FreePBX | FreePBX | Asterisk 的網頁管理介面；本專案用它管 userman 名冊 |
| Asterisk | Asterisk | 電話引擎；dialplan 與 AGI 的執行環境 |
| ElevenLabs | ElevenLabs | STT 服務供應商（批次 `scribe_v2`；Realtime `scribe_v3_live`） |
| userman | User Manager | FreePBX 的使用者目錄；本專案只讀 |
| FastAGI | FastAGI | AGI 的 TCP 協定實作；`agi_server.py` |
