#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""trace.py — 單句除錯：看一句話在每一層長成什麼樣子。

存在理由：`eval.py` 回答「整體指標如何」、`demo.py` 回答「固定案例長什麼樣」，
兩者都不方便回答「我剛好遇到一句奇怪的，它到底在哪一層歪掉」。

用法
----
    uv run trace.py "我要找業務部的王先生"          # 單句
    printf '王偉\n章家豪\n' | uv run trace.py        # stdin 一行一句
    uv run trace.py --json "章家豪"                  # 機器可讀
    uv run trace.py --prev "王先生" --attempt 2 "業務部的王偉"   # 模擬第二輪

選項
----
    --attempt N   目前第幾輪（預設 1；>=2 會走「已問過一輪」的分支）
    --prev S      前幾輪的逐字稿，依時間順序，可重複給
    --all         列出所有評分過的人，含被門檻砍掉的
    --json        輸出 JSON（給其他工具吃）
    --data PATH   名冊 JSON（預設 data/people.json）
    --top-k N     候選池上限（預設 20）

設計原則：**不重寫任何評分或決策邏輯**。所有數字都來自 dialname.core 的
`score_breakdown()` / `_search()` / `needs_clarification()` 與 dialname.decision 的
`decide()`；trace 只負責把中間值攤開來。
否則印出來的數字遲早跟正式流程漂移，那比沒有 trace 更糟。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dialname.core import (
    WEIGHTS,
    ScoreParts,
    confusable,
    fuzzy_syllable,
    has_routing_cue,
    load_directory,
    needs_clarification,
    pinyin_variants,
)
from dialname.decision import (
    HOMOPHONE_FLOOR,
    SINGLE_CANDIDATE_FLOOR,
    decide,
    plausible_options,
)

DEFAULT_DATA = Path(__file__).parent / "data" / "people.json"
RULE = "─" * 78
HEAVY = "━" * 78


def _f(x: float | None, nd: int = 3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _top_score(explain: dict | None) -> float | None:
    if not explain:
        return None
    kept = [r["score"] for r in explain["rows"] if r["kept"]]
    return max(kept) if kept else None


# --------------------------------------------------------------------------- #
# 人看的輸出
# --------------------------------------------------------------------------- #
def render(utt: str, history: list[str], tr: dict, decision, attempt: int,
           show_all: bool) -> str:
    ex = tr["bucket"] if tr["chosen"] == "bucket" else tr["full"]
    cands = tr["cands"]
    out: list[str] = []
    add = out.append

    add(HEAVY)
    add(f"「{utt}」    attempt={attempt}"
        + (f" · 歷史 {len(history)} 輪" if history else ""))
    add(RULE)

    # ① 正規化（NFKC → 簡轉繁 → 去標點）
    add(f"① normalize     「{ex['normalized']}」")
    # ② 贅詞 / 稱謂 / 虛詞
    removed = " / ".join(ex["noise_removed"]) or "（無）"
    add(f"② strip_noise   「{ex['cleaned']}」        剝除：{removed}")
    # ③ 部門
    add(f"③ dept_match    {ex['dept'] or '（無）'} → 殘留「{ex['dept_residual']}」")
    meta = tr.get("meta") or {}
    if meta.get("alias"):
        add("   ※ 只講名／小名：alias 同音命中（候選都帶 alias 標記，分數 0.95）")
    elif meta.get("given_like"):
        add("   ※ 只講名／小名：alias 沒有登記 → decide() 轉總機（alias-not-found）")
    # ④ 拼音
    variants = pinyin_variants(ex["dept_residual"]) if ex["dept_residual"] else []
    shown = " | ".join(" ".join(v) for v in variants[:4]) or "（空）"
    tail = "" if len(variants) <= 4 else f" …共 {len(variants)} 組"
    add(f"④ pinyin        tight: {shown}{tail}")
    if variants:
        add(f"                loose: {' '.join(fuzzy_syllable(s) for s in variants[0])}")
    # ⑤ 候選池（桶 vs 全掃）
    if not ex["dept_residual"]:
        note = f"殘留為空 → 不評分任何人（{ex['pool']} 池 0 人），走部門路由分支"
    elif tr["chosen"] == "full" and tr["full"] is not None:
        btop = _top_score(tr["bucket"])
        why = "桶路徑無候選" if btop is None else f"桶路徑 top={_f(btop)} 較低"
        note = f"全掃 {tr['full'].get('pool_size', 0)} 人（{why} → 改用全掃）"
    else:
        note = f"{ex['pool']} 路徑 {ex.get('pool_size', 0)} 人"
        if tr["full"] is not None:
            ftop = _top_score(tr["full"])
            tail = "全掃無候選" if ftop is None else f"全掃 top={_f(ftop)} 未超過"
            note += f"；{tail} → 採用桶路徑"
    if ex.get("dept_scoped"):
        note += "（部門收斂）"
    if ex.get("fallback"):
        note += "；觸發 fallback 全掃"
    add(f"⑤ 候選池        {note}")

    # ⑥ 候選與分數拆解
    rows = sorted(ex["rows"], key=lambda r: (-r["score"], r["pid"]))
    kept = [r for r in rows if r["kept"]]
    dropped = [r for r in rows if not r["kept"]]
    add(f"⑥ 候選          {len(kept)} 人過門檻／{len(rows)} 人評分"
        + ("" if not kept else
           ("（--all：全部列出）" if show_all else f"，列前 {min(len(kept), 5)}")))
    for i, r in enumerate(kept if show_all else kept[:5], 1):
        parts: ScoreParts = r["parts"]
        add(f"   {i}. {r['name']}（{r['dept']}）  score={r['score']:.4f}")
        add("      " + "  ".join(
            f"{k} {getattr(parts, k):.3f}×{WEIGHTS[k]:.2f}={v:.3f}"
            for k, v in parts.contributions().items()))
        extras = []
        if parts.override:
            extras.append(f"提分 {parts.override}")
        if parts.cap:
            extras.append(f"壓分 {parts.cap}")
        if r["dept_delta"]:
            extras.append(f"dept {r['dept_delta']:+.2f}")
        if r["title_delta"]:
            extras.append(f"title {r['title_delta']:+.2f}")
        if parts.variant:
            extras.append(f"異讀#{parts.variant}")
        add(f"      raw {parts.raw:.3f}" + ("  ·  " + " · ".join(extras) if extras else ""))
        if r["reasons"]:
            add(f"      reasons: {', '.join(r['reasons'])}")
    if show_all:
        for r in dropped:
            add(f"   ✗ {r['name']}（{r['dept']}）  score={r['score']:.4f}"
                f" < floor {r['floor']:.2f}（當作沒聽懂）")
    elif dropped:
        add(f"   （另有 {len(dropped)} 人低於門檻 {dropped[0]['floor']:.2f}；"
            f"最高者 {dropped[0]['name']} {dropped[0]['score']:.4f}，--all 可看）")

    # ⑦ 澄清判斷（門檻與 decide() 同一組常數，不在此另寫數字）
    ask, why = needs_clarification(cands, homophone_floor=HOMOPHONE_FLOOR)
    line = f"⑦ clarify       {'要問' if ask else '不問'}（{why}）"
    if len(cands) > 1:
        gap = cands[0].score - cands[1].score
        line += f" · top1−top2={gap:.4f}"
        sibling = confusable(cands[0].person, cands[1].person)
        line += f" · 同音兄弟={'是' if sibling else '否'}"
        if sibling:
            # 只寫「是」會誤導：還要看它有沒有過門檻，否則會以為守衛漏了
            line += f"（第二名 {cands[1].score:.3f} vs floor {HOMOPHONE_FLOOR:.2f}）"
    add(line)
    if ask and len(cands) == 1:
        # 本地說要問，但只有一個候選 → 決策層沒有「選」的問題，
        # 分數過下限就直轉，否則轉總機（見 ⑧）
        if cands[0].score >= SINGLE_CANDIDATE_FLOOR:
            add("   ※ 只有一個候選、分數過下限 → decide() 直接轉，不會真的問（純參考，見 ⑧）")
        else:
            add(f"   ※ 只有一個候選、分數 < 下限 {SINGLE_CANDIDATE_FLOOR:.2f}"
                " → decide() 轉總機，不會真的問（見 ⑧）")
    if decision.action in ("fallback", "hangup"):
        # 只講姓／稱謂或資訊不足 → fallback；無意圖 → hangup
        target = "fallback（轉總機）" if decision.action == "fallback" else "hangup（掛線）"
        add(f"   ※ decide() 判 {target}（{decision.reason}），不再追問")
    if ask:
        picks = plausible_options(cands)
        if picks:
            add(f"   可列選項：{', '.join(c.person.name for c in picks)}"
                "（僅 disclose_names=True 才會念出）")
    add(f"   路由意圖詞 {'有' if has_routing_cue(utt) else '無'}"
        f" · 整句只有部門名 {'是' if (ex['dept'] and ex['dept_residual'] == '') else '否'}")

    # ⑧ 決策
    add(f"⑧ decide        {decision.action}"
        + (f" → {decision.target}" if decision.target else "")
        + (f"（{decision.label}）" if decision.label else ""))
    add(f"   reason={decision.reason} · confidence={_f(decision.confidence)}")
    add(f"   speech={decision.speech or '（無）'}")
    add(f"   options={decision.options or '（無）'}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 單句處理
# --------------------------------------------------------------------------- #
def trace_one(directory, utt: str, history: list[str], attempt: int,
              top_k: int) -> tuple[dict, object]:
    """回傳 (render 需要的 tr, Decision)。兩者共用同一次搜尋與決策。"""
    tr = directory.search_trace(utt, top_k=top_k)
    ex = tr["bucket"] if tr["chosen"] == "bucket" else tr["full"]
    meta = tr.get("meta") or {}
    decision = decide(
        history + [utt], tr["cands"], tr["dept"],
        attempt=attempt,
        departments=directory.departments,
        dept_residual=ex["dept_residual"],
        surnames=directory.surnames,
        name_form="alias" if meta.get("alias") else (
            "given" if meta.get("given_like") else None),
    )
    return tr, decision


def to_json(utt: str, history: list[str], tr: dict, decision, attempt: int) -> dict:
    ex = tr["bucket"] if tr["chosen"] == "bucket" else tr["full"]
    ask, why = needs_clarification(tr["cands"], homophone_floor=HOMOPHONE_FLOOR)
    return {
        "utterance": utt,
        "history": history,
        "attempt": attempt,
        "search": {
            "chosen": tr["chosen"],
            "q_name": tr["q_name"],
            "dept": tr["dept"].name if tr["dept"] else None,
            "bucket_top": _top_score(tr["bucket"]),
            "full_top": _top_score(tr["full"]),
            "normalized": ex["normalized"],
            "noise_removed": ex["noise_removed"],
            "cleaned": ex["cleaned"],
            "dept_residual": ex["dept_residual"],
            "pool": ex["pool"],
            "pool_size": ex.get("pool_size"),
            "dept_scoped": ex.get("dept_scoped", False),
            "fallback": ex.get("fallback", False),
            "weights": WEIGHTS,
            "rows": [
                {**{k: v for k, v in r.items() if k != "parts"},
                 "parts": asdict(r["parts"]),
                 "contributions": r["parts"].contributions()}
                for r in sorted(ex["rows"], key=lambda r: (-r["score"], r["pid"]))
            ],
        },
        "candidates": [
            {"pid": c.person.pid, "name": c.person.name, "dept": c.person.dept,
             "extension": c.person.extension, "score": c.score, "reasons": c.reasons}
            for c in tr["cands"]
        ],
        "clarify": {"needed": ask, "why": why},
        "decision": asdict(decision),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="單句管線除錯（姓名 → 分機）")
    ap.add_argument("utterance", nargs="?", help="要追蹤的一句話；省略則從 stdin 讀（一行一句）")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--prev", action="append", default=[], metavar="S",
                    help="前幾輪的逐字稿，依時間順序，可重複給")
    ap.add_argument("--all", action="store_true", help="列出全部候選（含低於門檻）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--top-k", type=int, default=20)
    args = ap.parse_args(argv)

    if args.utterance is not None:
        lines = [args.utterance]
    else:
        lines = [ln.strip() for ln in sys.stdin.read().splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]

    if not lines:
        print('沒有輸入。用法：uv run trace.py "我要找王先生"', file=sys.stderr)
        return 2

    directory = load_directory(args.data)
    history = list(args.prev)
    payloads = []
    for i, utt in enumerate(lines):
        tr, decision = trace_one(directory, utt, history, args.attempt, args.top_k)
        if args.json:
            payloads.append(to_json(utt, history, tr, decision, args.attempt))
        else:
            if i:
                print()
            print(render(utt, history, tr, decision, args.attempt, args.all))
    if args.json:
        print(json.dumps(payloads if len(payloads) > 1 else payloads[0],
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
