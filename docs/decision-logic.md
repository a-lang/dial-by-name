# 決策邏輯：不變式、順序與門檻

本檔說明決策順序、評分與門檻取值，以及改動前必須維持的不變式。逐日取捨脈絡與真實評測紀錄
留在維護者的私有 archive；對外的變更紀錄見 [`CHANGELOG.md`](../CHANGELOG.md)。

## 1. 不變式

改任何東西之前先確認這七條還成立；違反其中一條就是設計改變，不是實作細節。

1. **決策順序不可調換**（§2 的十條）。順序本身就是規則。
2. **同音兄弟一律本地解**：跨部門問部門、同部門 fallback，不猜。
3. **「不需澄清」與「只有一個候選」優先於任何追問**。
4. **(d) 只問一次**：第二輪仍拉鋸 → `clarify-exhausted`；任何路徑都不得產生第三次追問。
5. **決策層零外部呼叫**：`decide()` 沒有委派參數，`dialname/` 內沒有任何通往外部 API 的路徑。
6. **部門路由鍵是 `dept_id`（ASCII）**，符合 `^[a-z][a-z0-9_]*$`；中文部門名只進 log／payload，
   不進檔名、dialplan、通道變數。
7. **入口只 import `dialname`**：`eval.py`／`trace.py`／`demo.py`／`agi_server.py`／`stt_batch.py`
   不得自行實作正規化、繁簡轉換、同音分群、評分、門檻或決策。要加能力就加到 `core`。

掛線政策（第 8 條行為約定，不是結構不變式）：無意圖／無回應 → `hangup`（先播 `goodbye`）；
已表達找人意圖但收斂不了 → 總機。

<a id="order"></a>
## 2. 決策順序（`decide()`）

一句話原則：**先把「問了也沒用」與「本地已能決定」的收掉，剩下的才問一句。**

括號內是 `Decision.reason`，順序即程式碼順序。

| # | 條件（第一輪） | 動作 | 為什麼排在這 |
|---|---|---|---|
| 0 | 無候選 + 無路由意圖 + 非整句部門名，且殘留不是「姓氏開頭 2–4 字」 | **掛線**（`no-intent`）；**部門有解析到時例外**：`ask`（`dept-scoped-ask`） | 不是找人／找部門，最便宜的判斷放最前面；不讓騷擾電話佔用總機。姓氏開頭視為「真的在找人，只是名冊沒查到」（或 STT 聽歪），改走重問；**部門存活＝STT 殘骸的實證特徵**，念回部門再問一次，第 2 輪仍無候選照規則轉總機 |
| 1 | 沒有候選 + 有部門 + 有路由意圖（或整句只有部門名） | 轉部門 queue（`dept-only`） | 有部門就是有目的地，不需要猜人 |
| 2 | 沒有候選（有找人意圖） | 重問（`no-candidate-retry`；第 2 輪 → `no-candidate-giveup` → 總機） | 可能是 STT 掉字，值得再給一次機會 |
| 3 | 只講姓（查詢剩 1 字），**不看候選數** | fallback（`surname-only`） | 只有姓沒有名就不知道是誰；「名冊只有一個這個姓」不是證據，STT 同音（張/章、鍾/鐘）會把它對到別人身上 |
| 4 | 漏字（完整姓名少一字），**且候選 ≥2** | fallback（`incomplete-name`）；唯一候選不攔，交給 #7 | 他已經講過全名，再講一次很可能得到同樣的逐字稿 |
| 5 | 需澄清，且候選同部門又彼此同音（只講名字者除外） | fallback（`homophone-unresolvable`） | 部門相同→問部門無效；彼此同音→問全名敵不過 STT |
| 6 | `needs_clarification() == False` | 轉 top-1（`direct:<why>`） | 已能確定，沒有理由再問 |
| 7 | 候選只有 1 個（`score ≥ SINGLE_CANDIDATE_FLOOR`） | 轉該人（`single-candidate`） | 沒有「選哪一個」的問題，問了也沒有選項可給；門檻之下走 `low-confidence-single` → fallback |
| 8 | `why == homophone-sibling`（第一輪） | 問部門（跨部門 `q-which-dept`）／同部門或無部門線索 `q-say-full-name` | 部門名是封閉詞彙，STT 對它遠比對姓名可靠 |
| 9 | 以上皆非（低分／拉鋸、候選 ≥2、非同音平手） | `ask`（`ambiguous-ask:<why>`） | 本地沒把握、但問一句仍有鑑別力 |

另有兩條插在前面的分支：

- **(a0b) 只講名／小名**（查詢首字不是名冊的姓）→ 只認 alias：命中 1 人 → `direct:confident`；
  命中多人 → `alias-ask`（第 2 輪 → `clarify-exhausted`）；查無 alias 但發音像某人的名 →
  `alias-not-found` → 總機；兩者皆非 → 照上表走。
  理由：姓名比對的每一項分數都建立在「字有對上」，一旦 STT 把整個詞換掉，字元重疊歸零，
  而殘留字可能讓另一個人拿到 `single-candidate` 被直轉。alias 是**明示登記的白名單**，
  猜的空間不存在。代價是名冊沒登記小名的人講小名一律進總機。
- **(b5) alias 同音群（≥2 人）** → `alias-ask`。候選 ≥2 且全部是 alias 命中時，
  部門分佈取**全部**候選，不做姓名相似度過濾——小名命中本身就是指名道姓。

**第二輪規則**（`attempt >= 2`）：候選 0 → `no-candidate-giveup` → 總機；候選 1 → 轉接；
候選 ≥2 且仍需澄清 → `clarify-exhausted` → 總機。**confident 或單一候選仍會 `route`**，
第二輪補說部門也仍會 `route_dept`——「不再追問」不等於「一律 fallback」。
理由：第二輪若補了部門，會用 `DBN_CANDS` 把第 1 輪候選收斂到該部門（剩 1 人就直接轉）；
沒有交集才走 `dept-only` 轉整個部門。沒補部門就等於沒有新資訊，再問一次只是同一題。

### 2.1 兩個最容易放錯的位置

- 把 #9 提到 #8 之前 → 同音平手會被糊成「一般的低分拉鋸」，問全名敵不過 STT，
  第二輪直接 `clarify-exhausted`；跨部門同音必須問**部門**才解得掉。
- 把 #9 提到 #6、#7 之前 → 已經確定的答案、或只有一個候選時也去問一句，
  換來的是延遲與一次無效追問。

### 2.2 同音的三種情況

| 情況 | 例 | 本地能做的 | 剩下交給誰 |
|---|---|---|---|
| **A. 真同音，只有一個人** | 名冊只有「張家豪」，STT 給「章家豪」 | 拼音相同 → 1.0 命中 | 直接轉 |
| **B. 真同音，兩個人都在** | 張家豪（工程部）、章家豪（財務部） | 兩個都高分，差距小 | **一定先澄清**（或看部門線索） |
| **C. 字面剛好命中另一個人** | 查「汪偉」，名冊同時有王偉（業務）與汪偉（客服） | top1 = 1.00、margin = 0.21，**看起來很有信心** | ⚠️ 最危險 |

C 是純 margin 檢查抓不到的。判斷條件因此不能只看分數差：

```python
if second is not None:      # 順序很重要：同音檢查必須排在「低信心」之前
    if second.score >= HOMOPHONE_FLOOR and confusable(top, second):
        return True, "homophone-sibling"
    if same_surname_homophone(top, second):      # 同姓＋名同音不同字（不看分數）
        return True, "homophone-sibling"
if top.score < CONFIDENT_SCORE:
    return True, "low-score"
```

`confusable()` 比對的是 tight 或 loose 拼音鍵的交集。這條把合成測資的
`silent_wrong` 從 1.3% 壓到 0。

<a id="scoring"></a>
## 3. 評分與門檻

### 3.1 公式

```
score = 0.20 × 漢字逐字相似
      + 0.16 × 漢字部分相似（query 較長時才有意義）
      + 0.30 × 姓的拼音吻合
      + 0.14 × 字數吻合度
      + 0.10 × 名的拼音吻合
      + 0.10 × 聲母序相似（抗韻母誤聽）

完整包含：query 內含全名 → ≥0.97；全名內含 query → 0.55 + 0.4×長度比
英文別名命中 → 0.95
單字查詢（只說姓）→ 上限 0.62；兩字查詢 vs 三字名 → 上限 0.88
部門：同部門 +0.10、不同部門 −0.08（core.DEPT_BONUS / DEPT_PENALTY）
職稱：query 含該人 title → +0.04（core.TITLE_BONUS）
```

優先序是**姓 > 字數 > 名**：中文姓只有幾百個，同音的姓（王/汪、張/章）幾乎等於同一個人，
所以 `sur` 給 0.30；名字發散、同音太多（偉/瑋/煒、美/媄），只能給 0.10；字數居中 0.14。

`_part_sim()` 對姓與名各自算 tight 與模糊音兩層（模糊音 ×0.9，只在合併後明顯更近時勝出，
如 林/凌、陳/程）。查詢的對齊有兩種：前 k 個音節＝姓（正常），或整串都是名字
（只講名字：`美玲` 對 `陳美玲`）——名的部分取較高者，否則只講名字會被誤切成「姓=美、名=玲」。

**完全沒有漢字重疊時，拼音項只給半權重**（`PINYIN_EVIDENCE_NO_CHAR = 0.5`）：保住
「STT 把整個姓名都聽錯字」的召回，同時不讓亂句靠拼音序相似混過門檻。

**字數是獨立評分項（0.14）**，有兩個紀律：

1. **只在「字有對上」時計分**：無條件給分會讓亂句（同為 3 字、拼音序相似但一個漢字都沒對上）
   被字數分推過 `MIN_SCORE`，變成「亂句抓到人」。
2. **查詢比姓名多字時再乘結構折扣** `(len_name/len_query) ** 2`（`LENGTH_SHRINK_POWER`）：
   模糊比對只看「字有沒有出現」、不看順序，`王智偉` 對 `王偉` 會給高字元相似，
   平方折扣後直接低於門檻。

反向（查詢比姓名短＝漏字／只講姓／只講名）**不套**折扣——那是資訊不足，
由決策層的 `surname-only`／`incomplete-name`／`is_given_name_query` 專門處理。

上限（cap）是刻意的：**「王先生」不可能唯一**，分數就必須長得像「不確定」，
讓下游的澄清邏輯接手，而不是讓某個王先生僥倖拿 0.99 被自動轉接。

### 3.2 門檻

| 常數 | 值 | 為什麼 |
|---|---|---|
| `WEIGHTS` | .20／.16／.30／.14／.10／.10 | §3.1 的優先序；靠 342 題消融與失敗清單調出來的，不是常識 |
| `MIN_SCORE` | 0.45 | 進候選池的下限；低於此的分數連「聽錯字」都撐不起來 |
| `HOMOPHONE_FLOOR` | 0.70 | 第二名的分數到多少才觸發「同音兄弟」檢查。掃描 0.45–0.85 的結果：0.45–0.75 之間誤轉率與確認率都相同（同音兄弟的分數集中在 0.85 上下），0.85 開始漏掉同音兄弟，所以取區間中段 |
| `CONFIDENT_SCORE` | 0.92 | 低於此一律不直轉。這條是「誤轉」與「多問一句」的交換點：對電話簿而言多問一句遠比轉錯人便宜 |
| `DEPT_BONUS` / `DEPT_PENALTY` | +0.10 / −0.08 | 只負責**排序**。實例：加權把同音的 `汪偉` 從 0.7867 抬到 0.9467，勝過字面命中的 `王偉` 0.92 |
| `DEPT_SCOPE_MIN` | 1 | 部門收斂的門檻：來電者一報部門就**先在該部門內找**（不是硬過濾，部門內沒人會退回全名冊）。為什麼是 1 而不是 2：部門名是封閉詞彙，STT 對它遠比對姓名可靠。設 2 時「該部門只有一個人」不會觸發收斂，那個唯一人選就不參與競爭，部門線索等於白給 |
| `SINGLE_CANDIDATE_FLOOR` | 0.70 | 唯一候選直轉的下限。低於此（例如只講小名而名冊沒有該 alias 的殘留分數）轉總機 |

單獨開部門加權或單獨開部門收斂都能救回同音歧義；兩個都留著是分工——加權負責排序、
收斂負責縮小候選池（500 人以上時省掉大部分打分）。若只要「純加權、不要硬收斂」，
把 `DEPT_SCOPE_MIN` 設大即可。

**改門檻的紀律**：門檻與權重是靠合成測資的回歸訊號調的，不是靠單一案例。
改動必須同時（a）L0 golden 的變化可以逐條解釋、（b）L1 劇本無漂移或漂移已逐條判定、
（c）`CHANGELOG.md` 記一筆。**不要為了讓某個測試過而放寬門檻**——先判斷是規則錯還是測資錯。

### 3.3 決策層只吐 clip key

`Decision.speech` 是 `sounds/zh/<name>` 形式的**標籤**，`agi._clip_name()` 會剝掉前綴，
變成 `DBN_SPEECH=<name>`；dialplan 再組 `Playback(${DBN_SOUND}/<name>)`，其中
`DBN_SOUND=/var/lib/asterisk/sounds/dbn/zh`。決策層不碰檔名與音檔存在性。
19 支台詞、`clip_hash`／manifest 與 `disclose_names=True` 的缺口見 [`sounds.md`](sounds.md)。

<a id="alias"></a>
## 4. 名冊來源與 alias 政策

### 4.1 兩個入口，同一個形狀

- `core.load_directory()`：JSON 名冊（`data/people.json`），離線開發與測試用。
- `pbx_directory`：正式入口，讀 FreePBX userman＋sidecar。契約與 JSON 版相同，
  所以切換與回滾都只是換入口（分層理由見 [`architecture.md`](architecture.md)）。

userman 的欄位對映只認結構化的 `fname`＋`lname`（`displayname` 只進 log，不當名稱來源）；
`default_extension` 是轉接目標，`department` 是自由文字、載入時解析成 `dept_id`，
非空但對不到 → 載入錯誤。**只收 `source_directory` 指定的一個 directory 的人**
（`auth` 不符者不進索引），避免跨目錄重複與測試目錄混入電話簿。逐欄來源見
[`glossary.md`](glossary.md) §1。

**姓名拆分用 `lname`／`fname`，不切字串**：複姓（歐陽明）、西方名（David Wang）、只有名（Cher）
三種情況用「顯示名第 1 個字＝姓」去猜都會錯；欄位沒有值時才退回 `name_key` 前後切。
`looks_incomplete_name()` 只對中文姓名生效：英文名掉一個字母是拼字錯誤，交給別名與模糊比對。

### 4.2 alias 是 sidecar，不是上游欄位

userman 只有姓名／分機／部門，沒有「小名／英文名」。所以「只講名」政策的 alias 來自
sidecar `data/aliases.json`（稀疏：只列需要小名的人；沒登記的人講小名 → 總機）。

- key 是 `userman_users.id`，**不是分機**（分機會被重複指派，離職者的號碼換人會讓 alias 無聲移轉）。
- 頂層 `source_directory` 必須**逐字**等於 `userman_directories.name`。
- 孤兒 alias（指向不存在或不在該 directory 的 user）會讓 reload **失敗並保留舊索引**。
- `scripts/check_aliases.py` 是唯讀檢查（孤兒／重複／空值／控制字元，非零退出碼），
  改檔後先跑它再 reload。
- 範本 `data/aliases.example.json` 進版控；正式檔含真名，**不入版控**、權限 600。

### 4.3 部門定義

`data/departments.json` 是 `Department[]` 的來源（userman 只有人的自由文字 department，
沒有部門清單）。`dept_id` 是 ASCII 鍵（`^[a-z][a-z0-9_]*$`），同時是音檔尾碼、dialplan label
與 queue 對映鍵；改 `dept_id` 要連 queue 對映與 `dept-<id>` 音檔一起動。

### 4.4 重載

索引重建**只**靠 AGI 的 SIGHUP reload（`systemctl reload dial-by-name-agi`／
`scripts/agi.sh reload`）——`fwconsole reload`（Apply Config）不影響名冊，因為 userman 不產生
Asterisk 設定。寫檔一律「寫暫存 → `mv` 取代」，避免半寫入檔案碰上 HUP。幾百人的索引重建 < 100 ms。

## 5. 決策層的外部模型邊界

500 人的候選清單同時造成三件事：token 成本與延遲隨人數線性上升；選項一多，機率分布被攤平、
`confidence` 失去鑑別度；同音字（王/汪、張/章）本來就只有**發音**能分辨，把判斷丟給語言模型
等於要它做字串比對——那是本地 3 ms 就能做完的事。所以分工是「本地索引收斂候選 → 本地決策」，
**整本電話簿永遠不出機器**（連少數候選也不外送，理由見 §6）。

## 6. 外部模型：移除的理由、重開條件

**2026-11-21 起決策層零外部模型存取**：`decide()` 沒有委派參數，`dialname/` 內沒有任何
通往外部 API 的路徑。外部服務只剩 STT（ElevenLabs Scribe v2），它聽到的本來就是來電者的語音。

原本的設計是在「本地收斂不了的多候選」那一段（也就是現在的 `ambiguous-ask:<why>`，#9）
把候選交給外部分類器挑一個。實測該分支在**全部現有語料的可達率是 0**：

| 語料 | 規模 |
|---|---|
| 合成測資 | 342 題 |
| 實撥第一輪＋第二輪 | 41＋8 通 |
| L1 劇本 | 20 通＋6 輪 |

掃描器是 `scripts/jev_probe.py scan`（零 live 呼叫，把 `decide()` 的 reason 映射成
L1／L2／L3／L4；只有 L4＝`ambiguous-ask:*` 是需要外部模型的那一段）。分佈是**結構性證據**：
由現行規則定義，規則／門檻／名冊變更後必須重跑。

另外兩個理由：同音是**發音層的平手**，任何分類器看到的 `phonetic_score` 都是同一組數字，
信心再高也只是「有自信地猜一個」；而「只把少數候選送出去」的邊界，靠本地規則就能守住。

重開條件（任一成立就重議，改動前先在 `CHANGELOG.md` 記錄）：

1. `uv run scripts/jev_probe.py scan --calls <真實通話語料>` 掃出 **≥1 筆 L4**；
2. 正式階段 30 通真實電話評測出現**低分多候選**案例；
3. 規則／門檻／名冊的變更使 `scan` 的 L4 分佈**脫離 0**。

重開＝照重建程序重新接回委派分支並**全部重新評估**（payload 契約、門檻、成本、個資面四項
一併歸零，不得沿用舊結論）；最小必要條件與私有 archive 的指向見契約 §3 與
[`AGENTS.md`](../AGENTS.md)。
