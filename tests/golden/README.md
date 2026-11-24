# golden — `eval.py` 的指標基準

`eval_summary.json` 是 342 題合成測資的 16 項指標快照。用途只有一個：
**規則改動後確認指標沒意外變動**。逐題明細（`eval_rows.json`，~99 KB）不入版控，
留在本機的 `out/`。

## 更新基準（只在你「刻意」改變行為時）

```bash
cd dial-by-name
uv run eval.py                                   # 先看數字是否如預期
cp out/eval_summary.json tests/golden/eval_summary.json
```

更新時要一起改 `CHANGELOG.md` 說明哪一項動了、為什麼。

## 比對

```bash
uv run eval.py
diff tests/golden/eval_summary.json out/eval_summary.json
```

逐題比對（需先有本機 baseline）：

```bash
uv run eval.py > out/baseline_stdout.txt
cp out/eval_rows.json out/baseline_rows.json
cp out/eval_summary.json out/baseline_summary.json
# 改完之後
uv run eval.py
diff out/baseline_stdout.txt out/after_stdout.txt
diff out/baseline_rows.json out/eval_rows.json
```

## 注意

- 這些數字來自**合成測資**（對照表模擬同音錯誤），是回歸訊號，不是準確率。
- `clarify_rate_easy` 等「不追求 1.0」的指標刻意不是滿分（`clarify_easy 0.23` 是設計值），
  不要為了讓 diff 乾淨去動門檻。
