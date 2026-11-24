# SPDX-License-Identifier: AGPL-3.0-or-later
"""eval.py — 用「模擬 STT 錯誤」的查詢集量測比對品質。

不是為了炫數字，而是為了在沒有真實錄音前就能回答兩個問題：
  1. 同音字錯誤會不會讓「正確的人」掉出前三名？
  2. 該問澄清的時候，分數差距是否真的會變小？

用法：python3 eval.py
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dialname.core import (  # noqa: E402
    Department,
    Person,
    homophone_groups,
    load_directory,
    name_match_keys,
    needs_clarification,
    normalize,
    to_simplified,
)

ROOT = Path(__file__).parent

# 真實 STT（Whisper / Scribe / Azure…）絕大多數輸出**簡體**中文。
# 所以測資預設一律先轉簡體，才叫「模擬 STT 輸出」；繁體只保留少量對照組
# （少數引擎輸出繁體、或使用者是打字輸入）。
# 轉換器本身在 core（`to_simplified()`），這裡不另開一個 OpenCC 實例。
# 測資分類（summarize 與門檻掃描共用）
EASY_KINDS = {"exact-sc", "exact-tc", "drop-char-sc", "english", "filler-sc"}
# 「本來就不該被多問」的類型；drop-char 不放進來（少一個字本來就有歧義）
STRICT_KINDS = {"exact-sc", "exact-tc", "english", "filler-sc"}
HARD_KINDS = {"homophone-sc", "surname-only-sc", "dept+surname-sc", "given-name-only-sc"}
# recall@3 另外納入「報了部門的同音題」：這種題有部門線索，本來就應該排得進前三
RECALL_KINDS = HARD_KINDS | {"homophone-dept-sc"}

# 台灣現場最常混的同音字對（也是 Scribe 8 kHz 最常聽錯的一組）
HOMOPHONES = {
    "王": ["汪"], "汪": ["王"], "張": ["章"], "章": ["張"],
    "林": ["凌", "靈"], "凌": ["林"], "陳": ["程", "宸"], "程": ["陳"],
    "蓉": ["容"], "容": ["蓉"], "豪": ["豪"], "偉": ["瑋", "煒"],
    "傑": ["杰"], "玲": ["伶"], "美": ["媄"],
}


# ── 「完全不是要找人的」測資 ────────────────────────────────────────────
# 為什麼要這一類：把來電者亂轉，比同音字選錯更嚴重。這裡要量的是
# 「系統會不會在完全無關的句子裡硬找到一個人」。
#
# 三種陷阱，故意混在一起：
#   1. 純噪音：問地址、訂便當、報修 —— 理論上不該有任何候選
#   2. 部門詞陷阱：句子含部門別名（業務／工程／客服／人事…），但意圖不是找人
#      → 允許它「轉部門」（dept 命中），但**不該抓到某個人**
#   3. 稱謂陷阱：「找王伯伯」這種日常稱呼，不是員工姓名
GARBAGE_SENTENCES = [
    # 1. 純噪音
    "請問你們地址在哪裡",
    "我要訂便當",
    "我的網路壞了",
    "今天天氣怎麼樣",
    "請問幾點下班",
    "我要問發票的事",
    "你們有停車場嗎",
    "幫我查一下訂單",
    "請問廁所在哪",
    "我想應徵工作",
    # 2. 部門詞陷阱（含 業務/財務/工程/客服/倉庫/人事 的別名）
    "我要談業務合作",
    "財務報表什麼時候出來",
    "我要應徵工程師",
    "客服態度很差我要投訴",
    "倉庫那邊有人嗎",
    "人事命令什麼時候公布",
]

# 3. 稱謂陷阱（不是員工姓名）另立一類：若命中同姓的人，算「可接受的模糊」
#    而不是徹底失敗，所以只做觀察、不計入 garbage 的硬指標。
#    格式：(句子, 該句提到的姓) —— 姓用來組出「可接受的 pid 集合」。
NICKNAME_TRAPS = [("我要找王伯伯", "王"), ("我找那個陳董", "陳")]


@dataclass
class Case:
    query: str
    kind: str
    expect: set[str]          # 可接受的 pid（同音群可能多於一個）
    dept_expect: str | None
    must_clarify: bool


def homophone_swap(name: str) -> str:
    for i, ch in enumerate(name):
        if ch in HOMOPHONES:
            return name[:i] + HOMOPHONES[ch][0] + name[i + 1:]
    return name


def build_cases(people: list[Person], departments: list[Department]) -> list[Case]:
    cases: list[Case] = []
    rnd = random.Random(42)

    # 同音群：同拼音的人視為同一個可接受集合（定義在 core.same_pinyin，不在此另寫一份）
    cluster_of = homophone_groups(people)

    for p in people:
        surname = p.surname_key or p.name[0]
        same_surname = {q.pid for q in people if q.surname_key == surname}
        d = next((x for x in departments if x.dept_id == p.dept_id), None)

        # 1. 簡體全名 —— 真實 STT 的主要情境
        cases.append(Case(to_simplified(p.name), "exact-sc", {p.pid}, None, False))
        # 2. 繁體全名 —— 對照組（少數引擎輸出繁體／人工輸入）
        if to_simplified(p.name) != p.name:
            cases.append(Case(p.name, "exact-tc", {p.pid}, None, False))
        # 3. 同音字替換 + 簡體（最真實的錯誤組合：聽錯字 + 輸出簡體）
        swapped = homophone_swap(p.name)
        if swapped != p.name:
            cases.append(Case(to_simplified(swapped), "homophone-sc", cluster_of[p.pid], None, True))
            # 3b. 同音字替換 **但有報部門**：STT 對「部門名」這種封閉詞彙比對姓名可靠，
            #     所以部門應該足以解掉同音歧義（top-1 就應該對，不需要再問）。
            if d is not None:
                cases.append(
                    Case(f"{to_simplified(d.aliases[0] if d.aliases else d.name)}的{to_simplified(swapped)}",
                         "homophone-dept-sc", {p.pid}, d.name, False)
                )
        # 4. 漏一個字 + 簡體
        if len(p.name) >= 3 and rnd.random() < 0.5:
            cases.append(
                Case(to_simplified(p.name[0] + p.name[2:]), "drop-char-sc", {p.pid}, None, False)
            )
        # 5. 姓氏 + 稱謂（先生/小姐 簡繁同形）
        cases.append(
            Case(f"{to_simplified(surname)}先生", "surname-only-sc", same_surname, None, len(same_surname) > 1)
        )
        if d is not None:
            # 6. 只講部門
            cases.append(Case(f"帮我接{to_simplified(d.aliases[0])}", "dept-only-sc", set(), d.name, False))
            # 7. 部門 + 姓氏 + 稱謂
            cases.append(
                Case(
                    f"我要找{to_simplified(d.name)}的{to_simplified(surname)}先生",
                    "dept+surname-sc",
                    same_surname,
                    d.name,
                    True,
                )
            )
        # 7b. 只講名字（不講姓）
        #     2026-11-16 政策：沒有姓氏的查詢只認 alias（小名）。名冊沒登記小名的人
        #     就是「查無此人 → 轉總機」，所以這類案例的期望值改成**空集合**，
        #     並改歸 `given-name-unregistered`——不能讓它在 recall 指標裡永遠算 miss，
        #     那會讓「政策造成的行為」看起來像「比對退步」。
        if len(p.name) >= 3:
            given = to_simplified(p.given_key or p.name[1:])
            gkeys = name_match_keys(given)
            alias_pids = {
                q.pid for q in people
                if any(gkeys & name_match_keys(a) for a in q.aliases)
            }
            if alias_pids:
                cases.append(Case(given, "alias-given", alias_pids, None, len(alias_pids) > 1))
            else:
                cases.append(Case(given, "given-name-unregistered", set(), None, False))
        # 8. 英文別名（ASCII，不受簡繁影響）
        eng = [a for a in p.aliases if a.isascii()]
        if eng:
            cases.append(Case(f"我想找{eng[0]}", "english", {p.pid}, None, False))
        # 9. 贅詞 + 簡體全名（注意「麻烦」是簡體）
        cases.append(Case(f"你好我要找{to_simplified(p.name)}麻烦一下", "filler-sc", {p.pid}, None, False))

    # 10. 完全不是要找人的句子：expect 留空，正確行為是「一個候選都不該有」。
    #     這一類不看 hit1/hit3（沒有目標），只看「有沒有硬抓到人」。
    for s in GARBAGE_SENTENCES:
        cases.append(Case(s, "garbage", set(), None, False))

    # 11. 稱謂陷阱：可以命中同姓的人（模糊是合理的），但必須進澄清、不能直接轉。
    for s, surname in NICKNAME_TRAPS:
        same_surname = {q.pid for q in people if q.surname_key == surname}
        cases.append(Case(s, "nickname-trap", same_surname, None, True))

    return cases


def run(cases: list[Case], directory, departments, top_k: int = 5,
        homophone_floor: float | None = None) -> dict:
    rows = []
    for c in cases:
        cands, dept, q = directory.search(c.query, top_k=top_k)
        ids = [x.person.pid for x in cands]
        top1 = ids[0] if ids else None
        hit1 = top1 in c.expect if c.expect else False
        hit3 = bool(set(ids[:3]) & c.expect) if c.expect else False
        dept_ok = None
        if c.dept_expect:
            expected = next(
                (x for x in departments if normalize(x.name) == normalize(c.dept_expect)), None
            )
            dept_ok = (
                dept is not None and expected is not None and dept.dept_id == expected.dept_id
            )
        margin = (cands[0].score - cands[1].score) if len(cands) > 1 else 1.0
        # 只講部門（dept-only）沒有「要不要澄清」的問題，flagged 留 null 避免誤讀。
        # 其餘（含 garbage）一律照 needs_clarification 的判定 —— 「沒抓到人」也應該
        # 被標成需要重問，而不是被誤標成 dept-only。
        if c.kind.startswith("dept-only"):
            flagged, why = None, "dept-only"
        else:
            kwargs = {} if homophone_floor is None else {"homophone_floor": homophone_floor}
            flagged, why = needs_clarification(cands, **kwargs)
        rows.append(
            dict(
                query=c.query, kind=c.kind, top1=top1, top1_name=cands[0].person.name if cands else None,
                top1_score=cands[0].score if cands else 0.0, margin=round(margin, 3),
                hit1=hit1, hit3=hit3, dept_ok=dept_ok, flagged=flagged, why=why,
                # 「有沒有抓到任何一個人」—— garbage 類沒有正確答案，只用這個判斷
                hit1_or_any=top1 is not None,
            )
        )
    return rows


def run_decisions(cases, directory, departments) -> list[dict]:
    """跑一次決策層，只為了量「garbage 會不會被亂轉」。

    eval 的主力是量比對層（search 的輸出），但「有沒有真的轉出去」是決策層的事，
    所以這裡刻意跨一層。決策全本地，沒有外部呼叫。
    """
    from dialname.decision import decide

    out = []
    for c in cases:
        cands, dept, q, meta = directory.search_meta(c.query)
        form = "alias" if meta.get("alias") else ("given" if meta.get("given_like") else None)
        dec = decide([c.query], cands, dept, attempt=1, departments=departments,
                     dept_residual=q, surnames=directory.surnames, name_form=form)
        # 第 1 輪若追問，模擬第 2 輪（逐字稿累積），用來驗「不會問第三次」
        action2 = None
        if dec.action == "ask":
            dec2 = decide([c.query, c.query], cands, dept, attempt=2,
                          departments=departments, dept_residual=q,
                          surnames=directory.surnames, name_form=form)
            action2 = dec2.action
        out.append(
            dict(query=c.query, kind=c.kind, action=dec.action, reason=dec.reason,
                 action2=action2, routed=dec.action in ("route", "route_dept"),
                 # 「查無此人 → 總機」的專用旗標：只認 alias-not-found 這個 reason，
                 # 才不會把 fallback 的其他原因（只講姓、漏字）算進來
                 not_found=dec.reason == "alias-not-found")
        )
    return out


def _split_pids(value) -> set[str]:
    if not value:
        return set()
    return {p for p in re.split(r"[,\s]+", str(value).strip()) if p}


def build_cases_from_transcripts(path) -> list[Case]:
    """逐字稿 JSON（stt_batch 或 --calls 轉出）→ 既有的 Case 清單。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Case(
            query=d["transcript"],
            kind=d["kind"],
            expect=_split_pids(d.get("expect_pid")),
            dept_expect=d.get("dept_expect") or None,
            must_clarify=d["kind"] in HARD_KINDS,
        )
        for d in data
    ]


def build_transcripts_from_calls(log_path, truth_csv, out_path) -> Path:
    """AGI 日誌 + truth.csv → 與 stt_batch 相同 schema 的 transcripts JSON。

    每個 call_id 取**第一個** `stt.text` 非空且 `action != "retry"` 的行；
    以 `Path(wav).name` 對 truth 的 `file`。對不到的通話會印出來，不靜默吞。
    """
    logs = Path(log_path)
    files = sorted(logs.rglob("*.jsonl")) if logs.is_dir() else [logs]
    by_call: dict[str, list[dict]] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            call_id = row.get("call_id")
            if call_id:
                by_call.setdefault(str(call_id), []).append(row)

    chosen: dict[str, dict] = {}
    for rows in by_call.values():
        rows.sort(key=lambda r: r.get("ts", ""))
        pick = next(
            (
                r for r in rows
                if (r.get("stt") or {}).get("text", "").strip()
                and (r.get("decision") or {}).get("action") != "retry"
            ),
            None,
        )
        if pick and pick.get("wav"):
            chosen[Path(pick["wav"]).name] = pick

    out: list[dict] = []
    with open(truth_csv, encoding="utf-8-sig", newline="") as f:
        for t in csv.DictReader(f):
            file = t.get("file", "")
            row = chosen.get(file)
            if row is None:
                print(f"  [skip] {file}: 找不到對應的 AGI 日誌（或該通只有 retry）")
                continue
            stt = row.get("stt") or {}
            out.append(
                {
                    "file": file,
                    "transcript": stt.get("text", ""),
                    "truth_text": t.get("truth_text", ""),
                    "expect_pid": t.get("expect_pid") or None,
                    "kind": t.get("kind", ""),
                    "dept_expect": t.get("dept_expect") or None,
                    "lang": stt.get("lang"),
                }
            )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {out_path}（{len(out)} 筆）")
    return out_path


def summarize(rows: list[dict], dec_rows: list[dict] | None = None) -> dict:
    def acc(kinds, key):
        sub = [r for r in rows if r["kind"] in kinds]
        return round(sum(1 for r in sub if r[key]) / len(sub), 3) if sub else None

    silent_wrong = [
        r for r in rows if not r["flagged"] and not r["hit1"] and r["kind"] != "dept-only-sc"
    ]

    # 決策層指標（需要 dec_rows）
    def dec_rate(kinds, flag):
        sub = [r for r in (dec_rows or []) if r["kind"] in kinds]
        return round(sum(1 for r in sub if r[flag]) / len(sub), 3) if sub else None

    def dec_no_third_ask():
        """第 1 輪追問的題目，第 2 輪是否還有再問（目標：0 題再問）。"""
        sub = [r for r in (dec_rows or []) if r["action"] == "ask"]
        return round(sum(1 for r in sub if r["action2"] == "ask") / len(sub), 3) if sub else None

    def dec_not_ask(kinds):
        """資訊不足的題型「不再追問」的比例（fallback/route 都算，只要不是 ask）。"""
        sub = [r for r in (dec_rows or []) if r["kind"] in kinds]
        return round(sum(1 for r in sub if r["action"] != "ask") / len(sub), 3) if sub else None

    return {
        "n": len(rows),
        "silent_wrong_rate": round(len(silent_wrong) / len(rows), 4),
        "top1_easy_sc": acc({"exact-sc", "drop-char-sc", "english", "filler-sc"}, "hit1"),
        "top1_easy_tc": acc({"exact-tc"}, "hit1"),
        "sc_vs_tc_gap": (
            round((acc({"exact-sc"}, "hit1") or 0) - acc({"exact-tc"}, "hit1"), 4)
            if acc({"exact-tc"}, "hit1") is not None
            else None
        ),
        "recall3_hard": acc(RECALL_KINDS, "hit3"),
        "top1_homophone": acc({"homophone-sc"}, "hit1"),
        # 同音 + 有報部門：部門識別能解掉多少同音歧義（目標 1.0）
        "homophone_dept_top1": acc({"homophone-dept-sc"}, "hit1"),
        "dept_acc": acc({"dept-only-sc"}, "dept_ok"),
        "clarify_rate_hard": acc(HARD_KINDS, "flagged"),
        "clarify_rate_easy": acc(STRICT_KINDS, "flagged"),
        # 完全不是要找人的句子裡，硬抓到人的比例（目標 0）—— 這比同音字選錯嚴重
        "garbage_false_match": acc({"garbage"}, "hit1_or_any"),
        # 稱謂陷阱（王伯伯／陳董）有沒有進澄清（而不是直接轉）
        "nickname_trap_clarify": acc({"nickname-trap"}, "flagged"),
        # 決策層：亂句有沒有被「轉出去」（轉人或轉部門都算失敗，目標 0）
        "garbage_false_route": dec_rate({"garbage"}, "routed"),
        # 決策層：合法「只講部門」有沒有真的轉到部門（目標 1，確認閘門沒擋掉正常流量）
        "dept_route_recall": dec_rate({"dept-only-sc"}, "routed"),
        # 決策層：只講姓／只有稱謂時不該再追問（目標 1，走 fallback 或直接轉）
        "no_requery_rate": dec_not_ask({"surname-only-sc", "nickname-trap", "drop-char-sc"}),
        # 決策層：第 2 輪仍歧義時不再問第三次（目標 0，改為 fallback）
        "third_ask_rate": dec_no_third_ask(),
        # 決策層：只講名但 alias 沒登記 → 應該轉總機（not-found），不是掛線也不是亂轉
        # （目標 1；名冊開始登記中文小名後，這批案例會被歸到 alias-given）
        "alias_not_found_rate": dec_rate({"given-name-unregistered"}, "not_found"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="dial-by-name 比對／決策指標")
    ap.add_argument("--transcripts", help="改用逐字稿 JSON（stt_batch 或 --calls 轉出）")
    ap.add_argument("--calls", help="AGI 日誌目錄／檔：先轉成 transcripts 再算指標")
    ap.add_argument("--truth", help="truth.csv（--calls 時必填）")
    ap.add_argument("--directory", default=str(ROOT / "data" / "people.json"),
                    help="名冊 JSON（對 userman 冊的通話傳 PBX snapshot）")
    ap.add_argument("--no-scan", action="store_true", help="跳過門檻掃描")
    ap.add_argument("--out-suffix", default="", help="輸出檔名後綴（例：_real）")
    args = ap.parse_args(argv)

    if args.calls and not args.truth:
        ap.error("--calls 需要 --truth")
    if args.transcripts and args.calls:
        ap.error("--transcripts 與 --calls 只能擇一")

    directory = load_directory(Path(args.directory))
    people, departments = directory.people, directory.departments

    if args.calls:
        args.transcripts = str(
            build_transcripts_from_calls(
                args.calls, args.truth,
                ROOT / "out" / f"transcripts{args.out_suffix}.json",
            )
        )

    cases = (
        build_cases_from_transcripts(args.transcripts)
        if args.transcripts
        else build_cases(people, departments)
    )
    if not cases:
        print("no cases：truth.csv 與 AGI 日誌對不起來（或全部都是 retry）", file=sys.stderr)
        return 1

    rows = run(cases, directory, departments)
    dec_rows = run_decisions(cases, directory, departments)
    summ = summarize(rows, dec_rows)

    print(f"cases: {summ['n']}")
    for k, v in summ.items():
        if k != "n":
            print(f"  {k:26s} {v}")

    if not args.no_scan:
        print("\n--- 門檻掃描：確認率 vs 誤轉率 ---")
        print("  floor | silent_wrong | clarify_hard | clarify_easy")
        # 這裡**不重算指標**：換一個 homophone_floor 重跑 run()，指標一律由 summarize() 產生。
        for floor in (0.45, 0.55, 0.65, 0.75, 0.85):
            s = summarize(run(cases, directory, departments, homophone_floor=floor))
            print(
                f"  {floor:5.2f} | {s['silent_wrong_rate']:12.3f} | "
                f"{s['clarify_rate_hard']:12.3f} | {s['clarify_rate_easy']:.3f}"
            )

    print("\n--- 失敗案例（top3 沒中 / 亂句抓到人） ---")
    for r in rows:
        if (r["kind"] in HARD_KINDS | {"exact-sc", "exact-tc"}) and not r["hit3"]:
            print(f"  [{r['kind']}] {r['query']!r} -> {r['top1_name']} ({r['top1_score']})")
        elif r["kind"] == "garbage" and r["hit1_or_any"]:
            print(f"  [garbage 誤抓] {r['query']!r} -> {r['top1_name']} ({r['top1_score']})")

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    rows_path = out / f"eval_rows{args.out_suffix}.json"
    summary_path = out / f"eval_summary{args.out_suffix}.json"
    decisions_path = out / f"eval_decisions{args.out_suffix}.json"
    rows_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    decisions_path.write_text(
        json.dumps(dec_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote out/{rows_path.name}, out/{summary_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
