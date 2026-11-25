# 20 通撥號劇本：協議與判準

> 角色：本檔是「20 通劇本協議與判準（合成冊場次）」的唯一來源；跨檔只指向、不複製。

> 機器判定在 `data/call_script.json`（單一來源）；**人**照著撥、照著判。
> 表格由 `uv run scripts/calls.py table` 產生，`tests/test_call_script.py::test_calls_md_table_in_sync`
> 會斷言它與 JSON 一致。
> 電話層安裝與手動驗收：`docs/dialplan.md` §4。

## 1. 回歸三層

回歸測試分三層（設計理由見 `docs/design.md` §回歸三層）：

| 層 | 跑什麼 | 何時跑 |
|---|---|---|
| L0 | 342 題合成測資（`tests/test_eval_golden.py`） | 每次 `uv run pytest` |
| **L1** | **本文 20 通劇本＋3 個 `l1_only`**（`tests/test_call_script.py`／`scripts/calls.py check`） | 每次 `uv run pytest`；改決策層後先跑 |
| L2 | 音檔重播（`scripts/calls.py replay LOG [--keyterms] [--ab]`） | 換 STT 模型或要驗真實音質時；按需跑、不進 CI（需 key、有 API 成本） |

改 `dialname/core.py`、`dialname/decision.py`、`dialname/agi.py` 之後，先讓 L0＋L1 綠，再考慮撥電話。
要撥的時候照 §2 逐通撥、照 §3 判。

`mode: auto` 的 18 通由機器判定（`calls.py check`）；`mode: manual` 的 2 通
（#19 沉默、#20 按 0）只看人工行為，不計入自動通過率。

## 2. 20 通表格（勿手改）

| # | id | kind | mode | 第一輪 | 第二輪 | 最後一輪預期 | 備註 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | exact-sc | exact-sc | auto | 我要找鄭博文 | — | route/direct:confident/3104 |  |
| 2 | homophone-dept-carry | homophone-sc | auto | 我要找汪偉 | 客服部 | route/direct:confident/2202 |  |
| 3 | homophone-dept-sc | homophone-dept-sc | auto | 業務部的汪偉 | — | route/direct:confident/2101 |  |
| 4 | drop-char-sc | drop-char-sc | auto | 張豪 | — | fallback/incomplete-name |  |
| 5 | surname-only-sc | surname-only-sc | auto | 王先生 | — | fallback/surname-only |  |
| 6 | given-name-only-sc | given-name-only-sc | auto | 美玲 | — | fallback/alias-not-found |  |
| 7 | dept-only-sc | dept-only-sc | auto | 幫我接業務部 | — | route_dept/dept-only/sales |  |
| 8 | dept-surname-sc | dept+surname-sc | auto | 業務部的王先生 | — | fallback/surname-only |  |
| 9 | filler-sc | filler-sc | auto | 你好我要找業務部的王偉麻煩一下 | — | route/direct:confident/2101 |  |
| 10 | garbage-lunch | garbage | auto | 你們便當訂了沒 | — | hangup/no-intent |  |
| 11 | alias-unique | alias-unique | auto | 阿明 | — | route/direct:confident/3102 |  |
| 12 | alias-homophone-cs | alias-homophone | auto | 小美 | 客服部 | route/direct:confident/2201 |  |
| 13 | alias-homophone-adm | alias-homophone | auto | 小美 | 行政部 | route/direct:confident/6101 |  |
| 14 | alias-floor | alias-floor | auto | 小米 | — | fallback/low-confidence-single |  |
| 15 | same-name-homophone-sc | same-name-homophone-sc | auto | 我要找李佳蓉 | 業務部 | route/direct:confident/2103 |  |
| 16 | homophone-second-turn | homophone-sc | auto | 資訊部王偉 | 王偉 | fallback/clarify-exhausted |  |
| 17 | single-candidate-floor-sc | single-candidate-floor-sc | auto | 我要找古廷瑋 | — | route/single-candidate/4105 | 0.70–0.92 唯一候選直轉是政策未定案；update 前須重議。 |
| 18 | dept-answer-wrong-sc | dept-answer-wrong-sc | auto | 我要找陳美玲 | 人事部 | fallback/no-candidate-giveup |  |
| 19 | silence | silence | manual | — | — | hangup/stt-empty | 兩次不出聲：決策層兩輪皆 retry/stt-empty，再見掛線由 dialplan 的 DBN_TRIES 決定（docs/dialplan.md §3.9）。 |
| 20 | dtmf-operator | dtmf-0 | manual | — | — | operator/dtmf-0 |  |

讀表注意事項：

- 兩輪案例的第一輪是澄清 `ask`；「最後一輪預期」是最後一輪的決策。逐輪驗證（含第二輪候選
  carry）由 `calls.py check`／pytest 負責，不必在電話上重算。
- 預期欄唸法是 `action/reason/target`；`fallback`／`hangup` 沒有 target。
- `l1_only` 的三個案例（`second-turn-empty-retry`、`two-empty-turns-hangup`、
  `stt-debris-dept-ask`）只跑決策層，不進撥號表：前兩個由 #19 實撥覆蓋電話層行為，
  第三個是 STT 殘骸句（部門存活、其餘聽壞）的回歸釘。
- #17 是 0.88 的唯一候選直轉，政策未定案；看到它漂移時先重議政策，不要直接改 `expect`。
- 表格的預期分機來自 `data/people.json`（合成名冊）。換成你的名冊後，`calls.py check`
  會整批漂移——那是預期行為，請用 `calls.py update` 在 diff 上確認後再接受。

## 3. 通過判準

### 3.1 判三層（順序不可跳）

1. **`action`／`reason`**：決策類別與原因必須完全相等。
2. **`target`**：`route` 的分機、`route_dept` 的 `dept_id`（ASCII）必須完全相等；
   `fallback`／`hangup` 應為空。
3. **`stt_status`**：只存在 JSONL（`out/calls/*.jsonl`）。非 `ok` 時**先看是不是重試路徑**
   （`stt-empty`／`stt-error`／`timeout`／`401`），不要拿它去解釋決策漂移。

決策漂移一律算失敗。電話層落點（`fallback` 轉總機、401／403 落總機）見 `docs/dialplan.md` §3.9 與
`docs/io-mapping.md` §3。

### 3.2 STT 崩壞（可操作定義）

不得用肉眼判斷「聽起來怪」就記 STT 崩壞。判定程序：

1. 取該通第一輪逐字稿，先過 `dialname.core` 的正規化（NFKC → 簡轉繁 → 去標點；
   真實 STT 吐簡體、劇本是繁體）。
2. 正規化後 **0 候選且部門解析失敗，且拼音對不上任何 auto 劇本台詞、位置也補不上**，才算
   STT 崩壞。0 候選的合法案例（如只講名）不得誤判。
   同音／近音變體一律標「STT 變體」，決策對錯照 expect 判（通過或漂移），不得因聽錯字就丟進崩壞。
   `compare` 已內建此判定；人工覆核時可用 `uv run trace.py "<逐字稿>"` 對同一份程式、同一組門檻。
3. 同句重撥，最多再 2 次（合計 3 次）；第 3 次仍崩壞才記「STT 限制」。
4. 重撥後通的句子**仍要單獨列在報表**（註明重撥次數），不得從結果消失。
5. 「STT 限制」不能成為決策漂移的合法出口：決策不同就是漂移，照 §6 記錄與處理。

### 3.3 分數層

只比**第一輪 top1**（JSON 的 `scores`）：差 ≥ 0.02 警告、> 0.10 失敗（`calls.py check` 與 pytest 同門檻）。
分數位移是觀察訊號，不翻案決策；決策正確但分數變薄時在場次紀錄註明。

### 3.4 「全過」的意思

- **全過**＝18 通 auto 的 `action`／`reason`／`target` 全中＋2 通 manual 行為正確。
- **全過 ≠ 覆蓋**：§5 列的項目即使全過也沒被驗到。

`uv run scripts/calls.py check` 全過時 exit 0；有漂移 exit 1。

### 3.5 manual 兩通怎麼判

- **#19 沉默**：第一次播 retry 提示音、第二次播 `goodbye` 後掛線（`DBN_TRIES` 整通上限 1，
  `docs/dialplan.md` §3.9）。JSON 的 `expect` 寫 `hangup/stt-empty` 是**決策層每輪的輸出**，
  實際掛線是 dialplan 的行為；兩者都要對，不是「決策層自己掛線」。
- **#20 按 0**：錄音期間按 0 → 總機。DTMF 由 dialplan 攔截，AGI 看不到；JSON 的
  `operator/dtmf-0` 是驗收目標，不是 `decide()` 的輸出。

## 4. truth.csv：14 筆

`truth.csv` 只服務 `eval.py --calls`（算 STT／檢索指標），**不是** 20 通的判準；20 通照 §3 判。
只收 #1–10、15–18。`calls.py truth-out` 會依撥號順序產生，`file` 欄位取自日誌的 wav 檔名；
**wav 檔名僅作 join key，實體檔案已即時刪除**（`docs/dialplan.md` §3.9 的 wav 即刪政策），
`eval.py --calls` 只讀 JSONL 的 `stt.text`，不需要音檔。
下表只列答案卡內容。實際 CSV 欄位是 `file,truth_text,expect_pid,kind,dept_expect`。

| # | truth_text | expect_pid | kind | dept_expect |
|---|---|---|---|---|
| 1 | 我要找鄭博文 | `u016` | `exact-sc` | — |
| 2 | 我要找汪偉 | `u002` | `homophone-sc` | 客服部 |
| 3 | 業務部的汪偉 | `u001` | `homophone-dept-sc` | 業務部 |
| 4 | 張豪 | `u004` | `drop-char-sc` | — |
| 5 | 王先生 | — | `surname-only-sc` | — |
| 6 | 美玲 | — | `given-name-only-sc` | — |
| 7 | 幫我接業務部 | — | `dept-only-sc` | 業務部 |
| 8 | 業務部的王先生 | — | `dept+surname-sc` | 業務部 |
| 9 | 你好我要找業務部的王偉麻煩一下 | `u001` | `filler-sc` | — |
| 10 | 你們便當訂了沒 | — | `garbage` | — |
| 15 | 我要找李佳蓉 | `u010` | `same-name-homophone-sc` | — |
| 16 | 資訊部王偉 | `u001,u002` | `homophone-sc` | — |
| 17 | 我要找古廷瑋 | `u040` | `single-candidate-floor-sc` | — |
| 18 | 我要找陳美玲 | `u008` | `dept-answer-wrong-sc` | — |

不收的 6 通：

- **#11–14（alias）**：屬 alias 政策驗收（`docs/dialplan.md` §4.2），不是評測資料。
  #12–13（小美）第一輪兩個候選同為 0.95，`expect_pid` 填單一人在 `top1` 會隨排序翻面，
  製造假的 `silent_wrong`；#11（唯一命中）／#14（下限）同理只作驗收證據。
- **#19–20（manual）**：沒有逐字稿，`eval.py --calls` 本來就跳過（只挑有 `stt.text` 的輪次）。

另外 **401／403** 是操作測試（把 `.env` 的 key 註解掉），不屬於這 14 筆，也不屬於劇本通數（§5）。

## 5. 未涵蓋清單

電話層的 16 項驗收（beep、各階段按 0、AGI 掛掉、`Dial` 失敗…）仍以 `docs/dialplan.md` §4.1 手動勾選。
以下是**決策層劇本測不到**的部分：

| 項目 | 為什麼 20 通沒覆蓋 |
|---|---|
| 401／403 | key 失效走 `fallback` → 總機（`docs/io-mapping.md` §3）；20 通都在 key 正常下撥。操作測試另行（`.env` 註解 key），判準見 `docs/dialplan.md` §4.2。 |
| `Dial` 失敗 | 分機不存在／忙線 → 轉總機。對 queue 目的地**不會觸發**（queue 立刻應答，`DIALSTATUS=ANSWER`），必須實撥才知道。 |
| `RECORD_STATUS=ERROR` | 錄音本身失敗，吃掉同一份 `DBN_TRIES` 預算；20 通假設錄音成功。除錯先看 JSONL 的 `wav_duration_ms` 是否 `null`。 |
| 英文 alias | 名冊有 `David Wang`／`Amy Chen`／`Kevin` 等英文 alias；20 通只驗中文 alias（阿明／小美／筱美／小米）與中文姓名，英文 STT 拼法與大小寫未驗。 |
| 同部門同音兄弟 | 部門收斂後仍有 ≥2 個同音候選的情況。目前示範名冊的同音對都刻意跨部門（王偉／汪偉、陳美玲／程美玲、林志明／凌志明），沒有同部門同音對，20 通無從覆蓋；真實名冊要另外抽驗。 |

## 6. 執行順序

前置：PBX 已同步本版程式並重啟 AGI；`docs/dialplan.md` §3.8 冒煙通過；本機
`uv run scripts/calls.py check` 已是 exit 0。決策層沒綠就撥，只會把程式問題記成 STT 問題。

1. **撥號前先設本次場次專屬日誌**（在 PBX 的部署樹 `/var/lib/asterisk/dial-by-name/`）。不要和生產日誌共用檔案：
   真實來電會插入、跨午夜會散成兩檔、重撥是新 `call_id`，`compare` 一定錯位。

   ```bash
   AGI_LOGS=out/calls/test-YYYYMMDD scripts/agi.sh restart
   scripts/agi.sh status
   tail -n 3 out/agi.log          # 應見 FastAGI listening
   ```

   > 場次目錄就是定案做法（沿用既有 `AGI_LOGS`，不新增 `DBN_LOG`、不改 `agi.py`）；
   > 檔名仍是 `YYYYMMDD.jsonl`，`compare` 對目錄會依檔名排序合併（跨午夜散兩檔也對齊）。
   > 正式模式＝手動 `scripts/agi.sh`；選用 systemd（見 README「服務管理與開機自啟」）時
   > 把 `--logs` 指到同一個場次目錄。

2. **照 §2 撥 20 通**（順序 1→20）：兩輪案例等追問再念第二輪；#19 兩次不出聲；#20 錄音中按 0。
   每通記下 `UNIQUEID`（JSONL 的 `call_id` 或 wav 檔名），重撥也記。

3. **拉回本機的場次日誌**（只拉日誌目錄；wav 不拉）：

   ```bash
   rsync -av asterisk@my-ipbx2025:/var/lib/asterisk/dial-by-name/out/calls/test-YYYYMMDD/ out/calls/test-YYYYMMDD/
   ```

   > **錄音檔即時刪除（隱私政策）**：`result` 與 TTL sweeper 在服務內就刪掉兩支 wav，
   > PBX 的 `recordings/` 沒有東西可拉，本步驟不拉 `recordings/`。場次目錄裡的
   > `asterisk-dbnp.log` 供電話層落點核對（`Dial`／`answered` 行），`agi.log` 是 AGI 端日誌。
   > 日誌先留著，別邊拉邊刪（漂移要回頭查原始行）。

4. **對劇本**：

   ```bash
   uv run scripts/calls.py compare out/calls/test-YYYYMMDD/YYYYMMDD.jsonl
   ```

   `compare` 輸出五類判定（通過／漂移／STT 崩壞／未跑到／manual）；對不上的 call 單獨列出，
   不靜默跳過。漂移照 §3 記錄與處理。

5. **產 `truth.csv`**：

   ```bash
   uv run scripts/calls.py truth-out --calls out/calls/test-YYYYMMDD/YYYYMMDD.jsonl
   ```

   缺任何一通會指名缺哪幾通、不寫半份檔；回溯日誌（非 20 通完整場次）會拒絕產生。

6. **算指標**（truth 就緒後）：

   ```bash
   uv run eval.py --calls out/calls/test-YYYYMMDD --truth recordings/truth.csv --out-suffix _calls
   ```

每次實撥的結果與漂移說明，記進 `CHANGELOG.md` 的 Unreleased 段落（或你自己的評測紀錄）。
