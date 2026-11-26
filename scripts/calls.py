#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""劇本回歸工具（L1 決策層＋L2 音檔重播）：`table`／`check`／`update`／`compare`／`truth-out`／`stats`／`replay`。

資料單一來源是 `data/call_script.json`（`docs/evaluation.md#regression`）。
人工撥號協議、通過判準與 truth.csv 規則見 `docs/calls.md`。

只 import `dialname` 套件與標準庫：比對與決策都在套件內，這支工具不重寫規則
（`AGENTS.md` 硬性契約 1）。逐字稿比對前一律先過 `dialname.core` 的正規化
（NFKC → 簡轉繁 → 去標點；真實 STT 常吐簡體），再用 core 的贅詞剝除、拼音鍵
（`name_match_keys`，含模糊音）與候選池對齊：同音變體視為同句並標「STT 變體」，
位置補位只接手錨點之間的空隙。

    uv run scripts/calls.py table     # 20 通撥號的 Markdown 表格（貼進 docs/calls.md）
    uv run scripts/calls.py check     # 跑 18 通 auto＋3 個 l1_only；漂移則 exit 1
    uv run scripts/calls.py update    # 刻意變更：把 expect 搬到新行為並列 diff（--dry-run 只印）
    uv run scripts/calls.py compare out/calls/test-20261119/20261119.jsonl
    uv run scripts/calls.py truth-out --calls out/calls/test-20261119/20261119.jsonl
    uv run scripts/calls.py stats out/calls/test-20261119/          # stt.ms 分佈與決策統計
    uv run scripts/calls.py replay out/calls/test-20261119/           # L2，需 ELEVENLABS_API_KEY
    uv run scripts/calls.py replay out/calls/test-20261119/ --ab      # 無／有 keyterms 兩輪
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
# `uv run scripts/calls.py` 會把 scripts/ 放進 sys.path[0]；補回專案根目錄才 import 到套件。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialname.agi import CARRY_SEP, handle_turn  # noqa: E402
from dialname.core import (  # noqa: E402
    dept_match,
    load_directory,
    name_match_keys,
    normalize,
    strip_noise,
)
from dialname.stt_client import SttAuthError, SttClient, SttError  # noqa: E402

SCRIPT_PATH = ROOT / "data" / "call_script.json"
DATA_PATH = ROOT / "data" / "people.json"
TRUTH_PATH = ROOT / "recordings" / "truth.csv"
ENV_PATH = ROOT / ".env"
REPLAY_DIR = ROOT / "out"
WAV = "/tmp/does-not-exist.wav"          # 只需讓 _wav_duration_ms() 回 None

# 分數層門檻與 pytest 一致（計畫 §7）：> 0.02 警告、> 0.10 失敗。
SCORE_WARN = 0.02
SCORE_FAIL = 0.10

# truth.csv 只收 #1–10、15–18（docs/calls.md §4）：alias 四通屬驗收證據、
# manual 兩通沒有逐字稿。用撥號順序的 1-based 編號當契約。
TRUTH_ROWS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 18)
TRUTH_COLUMNS = ("file", "truth_text", "expect_pid", "kind", "dept_expect")


class FakeStt:
    """只回固定逐字稿；不碰音檔、不碰網路。"""

    def __init__(self, text: str = ""):
        self.text = text

    def transcribe(self, path):
        return SimpleNamespace(text=self.text, language="zho")


def load_script(path: Path = SCRIPT_PATH) -> dict:
    if not path.is_file():
        raise SystemExit(f"找不到劇本：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# table
# --------------------------------------------------------------------------- #
def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _expect_text(expect: dict) -> str:
    text = f"{expect['action']}/{expect['reason']}"
    if expect.get("target"):
        text += f"/{expect['target']}"
    return text


def format_table(script: dict) -> str:
    rows = []
    for index, case in enumerate(script["calls"], start=1):
        turns = case["turns"]
        rows.append(
            [
                str(index),
                case["id"],
                case["kind"],
                case["mode"],
                turns[0] if turns else "—",
                turns[1] if len(turns) > 1 else "—",
                _expect_text(case["expect"]),
                case.get("note") or "",
            ]
        )

    header = ["#", "id", "kind", "mode", "第一輪", "第二輪", "最後一輪預期", "備註"]
    table = [header, ["---"] * len(header)] + rows
    return "\n".join("| " + " | ".join(_cell(c) for c in row) + " |" for row in table)


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def _top1_score(cands: str) -> float | None:
    """`pid:score&pid:score` → 第一個（最高分）分數；沒有候選回 None。"""
    if not cands:
        return None
    pid_score = cands.split(CARRY_SEP, 1)[0]
    return float(pid_score.rpartition(":")[2])


def run_case(case: dict, directory) -> tuple[dict, dict]:
    """依序跑完案例；第二輪把上一輪的 cands 原樣餵回去（測真實 carry）。

    回傳（最後一輪結果, 第一輪結果）。
    """
    if not case["turns"]:
        raise SystemExit(f"{case['id']} 是 auto 案例，卻沒有逐字稿")

    prev = None
    first = None
    result = None
    for attempt, text in enumerate(case["turns"], start=1):
        result = handle_turn(
            WAV, attempt,
            stt=FakeStt(text),
            directory=directory,
            prev_pids=prev,
        )
        if attempt == 1:
            first = result
        prev = result["cands"]
    return result, first


def _score_state(expect: dict, first: dict) -> tuple[str, str]:
    """回傳（顯示文字, 等級）；等級是 ok／warn／fail。"""
    want_scores = expect.get("scores") or []
    got = _top1_score(first["cands"])
    if not want_scores:
        return ("0 候選", "ok") if got is None else (f"0→{got:.3f}", "fail")
    want = want_scores[0]
    if got is None:
        return f"{want:.3f}→0 候選", "fail"
    diff = round(got, 3) - want
    text = f"{want:.3f}→{got:.3f}（{diff:+.3f}）"
    if abs(diff) > SCORE_FAIL:
        return text, "fail"
    if abs(diff) >= SCORE_WARN:
        return text, "warn"
    return text, "ok"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def check(script: dict, directory) -> int:
    cases = [c for c in script["calls"] if c["mode"] == "auto"] + script["l1_only"]
    manual = [c["id"] for c in script["calls"] if c["mode"] == "manual"]

    rows = []
    drift = []
    score_bad = []
    for case in cases:
        result, first = run_case(case, directory)
        expect = case["expect"]
        want = (expect["action"], expect["reason"], expect["target"])
        got = (result["action"], result["reason"], result["target"] or None)
        score_text, score_level = _score_state(expect, first)

        if got != want:
            verdict = "漂移"
            drift.append((case, want, got))
        elif score_level == "fail":
            verdict = "分數失敗"
            score_bad.append((case, score_text))
        elif score_level == "warn":
            verdict = "警告"
        else:
            verdict = "通過"

        rows.append(
            [
                case["id"],
                f"{want[0]}/{want[1]}/{want[2] or '-'}",
                f"{got[0]}/{got[1]}/{got[2] or '-'}",
                score_text,
                verdict,
            ]
        )

    header = ["案例", "預期", "實際", "第一輪分數", "判定"]
    widths = [
        max(_width(row[i]) for row in [header] + rows) for i in range(len(header))
    ]
    print("  ".join(_pad(h, w) for h, w in zip(header, widths)).rstrip())
    for row in rows:
        print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())

    warned = sum(row[4] == "警告" for row in rows)
    failed = len(drift) + len(score_bad)
    print(
        f"\n{len(rows)} 通（{len(cases) - len(script['l1_only'])} 撥號＋"
        f"{len(script['l1_only'])} l1_only）：{len(rows) - failed - warned} 通過、"
        f"{warned} 警告、{len(drift)} 漂移、{len(score_bad)} 分數失敗"
    )
    if manual:
        print(f"manual（不跑，人工驗收）：{'、'.join(manual)}")

    if drift:
        print("\n漂移案例（逐字稿 → 預期 vs 實際）：")
        for case, want, got in drift:
            print(f"  {case['id']}：{case['turns']}")
            print(f"    預期 {want}／實際 {got}")
    if score_bad:
        print("\n分數失敗（第一輪 top1 差 > 0.10）：")
        for case, score_text in score_bad:
            print(f"  {case['id']}：{score_text}")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# update：刻意變更時把 expect 搬到新行為（計畫 §5、§7）
# --------------------------------------------------------------------------- #
def _dump_script(script: dict) -> str:
    """序列化回 `data/call_script.json` 的既有格式。

    `update` 只該動 `expect`，所以格式必須能逐位元重現原檔：`turns`／`truth`／
    `expect` 這種單層值維持一行（JSON 的 compact 形式），其餘物件兩格縮排。
    這樣 commit 的 diff 只會落在 expect 那幾行。
    """
    lines = ["{"]
    items = list(script.items())
    for i, (key, value) in enumerate(items):
        tail = "," if i < len(items) - 1 else ""
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            lines.append(f'  {json.dumps(key, ensure_ascii=False)}: [')
            for j, case in enumerate(value):
                lines.append("    {")
                fields = list(case.items())
                for k, (name, cell) in enumerate(fields):
                    comma = "," if k < len(fields) - 1 else ""
                    lines.append(
                        f'      {json.dumps(name, ensure_ascii=False)}: '
                        f"{json.dumps(cell, ensure_ascii=False)}{comma}"
                    )
                lines.append("    }" + ("," if j < len(value) - 1 else ""))
            lines.append("  ]" + tail)
        else:
            lines.append(
                f'  {json.dumps(key, ensure_ascii=False)}: '
                f"{json.dumps(value, ensure_ascii=False)}{tail}"
            )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _new_expect(result: dict, first: dict, old: dict) -> dict:
    """新 expect：決策取最後一輪，`scores` 取第一輪 top1（3 位小數）。

    舊 expect 沒有 `scores` 時不補（維持原本欄位集合）；manual 案例不走這裡。
    """
    expect = {
        "action": result["action"],
        "reason": result["reason"],
        "target": result["target"] or None,
    }
    if "scores" in old:
        score = _top1_score(first["cands"])
        expect["scores"] = [] if score is None else [round(score, 3)]
    return expect


def _expect_line(expect: dict) -> str:
    text = f"{expect['action']}/{expect['reason']}/{expect.get('target') or '-'}"
    if "scores" in expect:
        scores = expect.get("scores") or []
        text += f"（{scores[0]:.3f}）" if scores else "（0 候選）"
    return text


def update(script: dict, directory, path: Path = SCRIPT_PATH, *,
           dry_run: bool = False) -> int:
    """重跑 L1 並把 `expect` 搬到實算結果；台詞、truth、kind、mode 一律不動。

    範圍＝18 通 auto＋`l1_only`；manual（truth 為 null、人工驗收）不跑。
    沒變化的通不列；有搬移才寫檔，`--dry-run` 一律只印 diff。
    """
    cases = [c for c in script["calls"] if c["mode"] == "auto"] + script["l1_only"]
    manual = [c["id"] for c in script["calls"] if c["mode"] == "manual"]

    moved = 0
    unchanged = 0
    for case in cases:
        result, first = run_case(case, directory)
        old = case["expect"]
        new = _new_expect(result, first, old)
        if new == old:
            unchanged += 1
            continue
        moved += 1
        print(f"  {case['id']}：{_expect_line(old)} → {_expect_line(new)}")
        case["expect"] = new

    print(f"\n{moved} 通搬移、{unchanged} 通不變")
    if manual:
        print(f"manual（不跑，人工驗收）：{'、'.join(manual)}")
    if dry_run:
        print("--dry-run：未寫檔")
    elif moved:
        Path(path).write_text(_dump_script(script), encoding="utf-8")
    return 0


# --------------------------------------------------------------------------- #
# compare：測試場次日誌 → 逐通對劇本
# --------------------------------------------------------------------------- #
@dataclass
class LoggedCall:
    """一個 `call_id` 的所有輪次（已依 ts 排序；同秒維持檔案順序）。"""

    call_id: str
    records: list[dict]

    @property
    def turns(self) -> list[dict]:
        """真正有講話的輪次：空逐字稿與 `retry` 是重試路徑，不算劇本的一輪。"""
        return [
            r for r in self.records
            if _text(r) and _decision(r).get("action") != "retry"
        ]

    @property
    def final(self) -> dict | None:
        """最後一輪有講話的紀錄；整通沒講話時退回最後一筆（retry／401）。"""
        if self.turns:
            return self.turns[-1]
        return self.records[-1] if self.records else None

    @property
    def first_text(self) -> str:
        for r in self.turns:
            return _text(r)
        for r in self.records:
            if _text(r):
                return _text(r)
        return ""

    @property
    def first_wav(self) -> str:
        """第一輪有講話的 wav 檔名；truth.csv 的 `file` 就取這個。"""
        for r in self.turns or self.records:
            if r.get("wav"):
                return Path(r["wav"]).name
        return ""


def _text(record: dict) -> str:
    return ((record.get("stt") or {}).get("text") or "").strip()


def _decision(record: dict) -> dict:
    return record.get("decision") or {}


def load_log(log_path: str | Path) -> list[LoggedCall]:
    """單檔或目錄（依檔名排序合併全部 `*.jsonl`）→ 依 call_id 重組的通話清單。

    跨午夜會散成兩個檔；同一個 call_id 的輪次合併後再依 ts 排序，所以仍對得起來。
    `ts` 只有秒精度，同秒時維持檔案內順序（seq）。
    """
    path = Path(log_path)
    if path.is_dir():
        files = sorted(path.rglob("*.jsonl"), key=lambda p: (p.name, str(p)))
        if not files:
            raise SystemExit(f"{path} 裡沒有 *.jsonl")
    elif path.is_file():
        files = [path]
    else:
        raise SystemExit(f"找不到日誌：{path}")

    grouped: dict[str, list[tuple[str, int, dict]]] = {}
    seq = 0
    for file in files:
        for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{file}:{lineno} 不是合法 JSON：{exc}") from exc
            call_id = row.get("call_id")
            if not call_id:
                # 舊日誌可能沒有 call_id：用 wav 檔名去掉 -<attempt> 當替代鍵。
                stem = Path(row.get("wav") or "").name
                call_id = stem.rsplit("-", 1)[0] if "-" in stem else stem
            key = str(call_id) if call_id else f"(no-id-{seq})"
            grouped.setdefault(key, []).append((row.get("ts", ""), seq, row))
            seq += 1

    calls = []
    for call_id, entries in grouped.items():
        entries.sort(key=lambda e: (e[0], e[1]))
        calls.append(LoggedCall(call_id, [row for _, _, row in entries]))
    return calls


def _key(text: str) -> str:
    """比對鍵：core 正規化（含簡→繁）＋剝贅詞；空字串不可拿來比對。"""
    return strip_noise(normalize(text))[0]


def _top1(text: str, directory) -> tuple[str | None, str | None]:
    cands, dept, _q, _meta = directory.search_meta(normalize(text))
    return (cands[0].person.pid if cands else None, dept.dept_id if dept else None)


def _same_sound(want: str, got: str) -> bool:
    """兩句是否同音／近音。

    只呼叫 `core.name_match_keys`（字面＋tight 拼音＋模糊音 n/ng、zh/z…）取交集，
    不重寫拼音規則。比兩種視圖：
      - 剝贅詞後的鍵（去掉「呃／請問／麻煩一下」，一般對齊用）
      - 只做 `normalize` 的全句（`好` 會被 `strip_noise` 當虛詞剝掉，
        張好→張 會讓拼音比對失效，需要原句視圖補回來）
    真實 STT 的同音字（汪維→汪偉、張好→張豪）靠這一層視為同句。
    """
    for a, b in ((_key(want), _key(got)), (normalize(want), normalize(got))):
        if a and b and name_match_keys(a) & name_match_keys(b):
            return True
    return False


def _dept_only_match(want: str, got: str, directory) -> bool:
    """只講部門的輪次：兩邊都沒有候選，退而比同一組 dept_id（IT部／資訊部這種）。"""
    a_pid, a_dept = _top1(want, directory)
    b_pid, b_dept = _top1(got, directory)
    return not a_pid and not b_pid and bool(a_dept) and a_dept == b_dept


def _turn_match(script_text: str, log_text: str, directory) -> bool:
    """劇本的一輪 vs 日誌的一輪：字面 → 同音／近音 → 只講部門。

    真實 STT 的同音字（汪維→汪偉、張好→張豪、李家榮→李佳蓉）不靠位置硬湊：
    拼音相同就算同句，並標成「STT 變體」。位置補位只在錨點之間接手（見
    `_fill_positional_gaps`）。
    """
    a, b = _key(script_text), _key(log_text)
    if a and a == b:
        return True
    if _same_sound(script_text, log_text):
        return True
    return _dept_only_match(script_text, log_text, directory)



def _sequence_match(case: dict, call: LoggedCall, directory) -> bool:
    script_turns = case["turns"]
    if len(script_turns) != len(call.turns):
        return False
    return all(
        _turn_match(want, _text(record), directory)
        for want, record in zip(script_turns, call.turns)
    )


def _case_anchor(case: dict, call: LoggedCall, directory) -> bool:
    """逐輪內容錨點：call 講的每一輪都依序對得上劇本（可只講到前幾輪就轉走）。

    允許比劇本短（#2 第一輪就直轉），不接受比劇本長：多出來的輪次是劇本外
    的對話（或重試殘留），交給位置補位判定。
    """
    turns = case["turns"]
    spoken = call.turns
    if not turns or not spoken or len(spoken) > len(turns):
        return False
    return all(
        _turn_match(want, _text(record), directory)
        for want, record in zip(turns, spoken)
    )


def align_calls(script: dict, calls: list[LoggedCall], directory) -> tuple[dict, set]:
    """劇本 case（1-based 編號）↔ 日誌 call（0-based）的內容錨點。

    依撥號順序掃劇本，先讓逐輪內容對得上的 call 認領 case；同一 case 有多通
    候選（重撥、或同音變體撞到另一題的台詞）時取**最接近撥號位置**的一通。
    重撥是新 call_id，所以同一劇本只會被認領一次，其餘留在未對清單裡被列出。
    錨點之間的空隙由 `_fill_positional_gaps` 依撥號順序補位。
    """
    auto_pos: dict[int, int] = {}
    pos = 0
    for ci, case in enumerate(script["calls"], start=1):
        if case["mode"] == "auto":
            auto_pos[ci] = pos
            pos += 1

    pairs: dict[int, int] = {}
    claimed: set[int] = set()
    for ci, case in enumerate(script["calls"], start=1):
        if case["mode"] != "auto" or not case["turns"]:
            continue
        matches = [
            li for li, call in enumerate(calls)
            if li not in claimed and _case_anchor(case, call, directory)
        ]
        if matches:
            li = min(matches, key=lambda idx: (abs(idx - auto_pos[ci]), idx))
            pairs[ci] = li
            claimed.add(li)
    return pairs, claimed



def _ordered_session(pairs: dict[int, int]) -> bool:
    """內容對上的 call 是否照劇本順序出現。

    測試場次專屬日誌會是；歷史日誌（11-18 驗收）只撥了部分劇本、順序也不同，
    這種回溯模式不做位置補位，免得把驗收通話誤判成漂移（計畫 §5、§9 #5）。
    """
    ordered = sorted(pairs.items(), key=lambda kv: kv[1])
    return len(ordered) >= 2 and all(
        a[0] < b[0] for a, b in zip(ordered, ordered[1:])
    )


def _fill_positional_gaps(
    script: dict,
    calls: list[LoggedCall],
    pairs: dict[int, int],
    claimed: set[int],
) -> None:
    """有序場次：錨點之間（含頭尾）call 與 case 一對一時依撥號順序補位。

    這是「同音變體以外」的內容（STT 崩壞、講反順序）唯一能知道本來要撥哪一通
    的方式；補位只影響報表歸屬與 truth.csv 的 wav 對應，不影響「STT 崩壞」的
    可操作定義（補得上就不算崩壞）。回溯日誌不做，免得把驗收通話誤判成漂移。
    尾端也補 manual（#19 沉默、#20 按 0）的實撥通，讓 20 通 call 都有歸屬。
    """
    if not _ordered_session(pairs):
        return
    anchors = sorted(pairs.items(), key=lambda kv: kv[1])
    call_bounds = [-1] + [li for _, li in anchors] + [len(calls)]
    case_bounds = [0] + [ci for ci, _ in anchors] + [len(script["calls"]) + 1]
    for k in range(len(call_bounds) - 1):
        gap_calls = [
            i for i in range(call_bounds[k] + 1, call_bounds[k + 1])
            if i not in claimed
        ]
        gap_cases = [
            ci for ci in range(case_bounds[k] + 1, case_bounds[k + 1])
            if ci not in pairs
        ]
        # 數量不等代表中間有重撥／雜音，位置不可信：整段留給「未對上」清單。
        if len(gap_calls) != len(gap_cases):
            continue
        for ci, li in zip(gap_cases, gap_calls):
            pairs[ci] = li
            claimed.add(li)



def _is_stt_broken(call: LoggedCall, script: dict, directory) -> bool:
    """docs/calls.md §3.2 的可操作定義（2026-11-19 收緊）。

    正規化後 **0 候選且部門解析失敗**，**且**逐字稿的拼音對不上任何 auto 劇本
    台詞，才算 STT 崩壞。同音／近音變體（美林↔美玲、張好↔張豪）是合法輸入，
    不能記成崩壞；位置補得上的通也已被 case 認領，不會走到這裡。
    """
    text = call.first_text
    if not text:
        return False
    cands, dept, _q, _meta = directory.search_meta(normalize(text))
    if cands or dept is not None:
        return False
    for case in script["calls"]:
        if case["mode"] == "auto" and case["turns"] and _case_anchor(case, call, directory):
            return False
    return True


def _variant(case: dict, call: LoggedCall, ci: int, script: dict, directory) -> bool:
    """逐字稿是不是劇本台詞的同音／近音變體（或位置補位上的別題台詞）。

    只有「內容有差、但發音對得上（或只講部門的輪次部門相同）」才算；
    #16 那種把第二輪台詞先講、部門答錯的順序不同不算變體。
    位置補位的通如果在逐字稿裡講了**另一題**的台詞（小米↔小美），標成變體。
    """
    turns, spoken = case["turns"], call.turns
    mismatch = False
    for want, record in zip(turns, spoken):
        got = _text(record)
        if _key(want) == _key(got):
            continue
        if _same_sound(want, got) or _dept_only_match(want, got, directory):
            return True
        mismatch = True
    if not mismatch:
        return False
    for oi, other in enumerate(script["calls"], start=1):
        if oi == ci or other["mode"] != "auto":
            continue
        for line in other["turns"]:
            # 只認字面相同（小米↔小美 這種位置補位）；同音會把「王偉」撞到 #2
            # 「我要找汪偉」，把 #16 的順序問題誤標成變體。
            if any(_key(line) == _key(_text(r)) for r in spoken):
                return True
    return False


def _variant_text(case: dict, call: LoggedCall) -> str:
    """給報表用的第一組「劇本句 → 逐字稿」。"""
    for want, record in zip(case["turns"], call.turns):
        got = _text(record)
        if _key(want) != _key(got):
            return f"{want} → {got}"
    return ""


def _first_has_candidates(call: LoggedCall, directory) -> bool:
    """第一輪講話在名冊裡有沒有候選（0 候選＝最容易被誤判成 STT 崩壞的那種）。"""
    text = call.first_text
    if not text:
        return False
    cands, _dept, _q, _meta = directory.search_meta(normalize(text))
    return bool(cands)



def _stt_status(call: LoggedCall) -> str:
    bad: list[str] = []
    for record in call.records:
        status = str(record.get("stt_status") or "ok")
        if status != "ok" and status not in bad:
            bad.append(status)
    return "、".join(bad)


def _display_expect(expect: dict) -> str:
    text = expect["action"]
    if expect.get("target"):
        text += f" {expect['target']}"
    elif expect["action"] in {"fallback", "hangup", "retry"} and expect.get("reason"):
        text += f" {expect['reason']}"
    return text


def _actual_text(call: LoggedCall, case: dict | None = None) -> str:
    want = _want_tuple(case) if case else None
    parts = []
    for record in call.turns:
        d = _decision(record)
        text = d.get("action") or "?"
        target = d.get("target")
        reason = d.get("reason")
        if target:
            text += f" {target}"
        elif text in {"fallback", "hangup", "retry", "operator"} and reason:
            text += f" {reason}"
        # route 的 reason 平常省略；與預期不同時補在括號裡，否則「route 2202」兩欄一樣看不出漂移。
        if want and target and reason and reason != want[1]:
            text += f"（{reason}）"
        parts.append(text)
    return " → ".join(parts) if parts else "—"


def _score_note(case: dict, call: LoggedCall) -> str:
    want = (case["expect"].get("scores") or [None])[0]
    if want is None or not call.turns:
        return ""
    cands = call.turns[0].get("candidates") or []
    got = cands[0].get("score") if cands else None
    if got is None:
        return f"（分數 {want:.3f}→0 候選）"
    got = round(float(got), 3)
    if abs(got - want) < 0.001:
        return ""
    return f"（分數 {want:.3f}→{got:.3f}）"


def _hint_case(call: LoggedCall, script: dict, directory) -> tuple[int | None, str]:
    """未對上的 call 疑似哪一通劇本（第一輪對得上）；純提示，不參與判定。"""
    if not call.turns:
        return None, ""
    first = _text(call.turns[0])
    for ci, case in enumerate(script["calls"], start=1):
        if case["mode"] != "auto" or not case["turns"]:
            continue
        if _turn_match(case["turns"][0], first, directory):
            done, want = len(call.turns), len(case["turns"])
            if done < want:
                return ci, f"只跑到第 {done} 輪（劇本 {want} 輪）"
            if done > want:
                return ci, f"多出 {done - want} 輪"
            return ci, "逐字稿序列不同"
    return None, ""


def _extra_label(call: LoggedCall, script: dict, directory) -> str:
    if not call.turns:
        return "STT 空（空逐字稿；若本通是 #19 沉默則屬預期，人工判定）"
    if _is_stt_broken(call, script, directory):
        status = _stt_status(call)
        return f"STT 崩壞{f'（{status}）' if status else '（0 候選、部門未解析、拼音對不上劇本）'}"
    ci, note = _hint_case(call, script, directory)
    if ci is None:
        return "對不上"
    case = script["calls"][ci - 1]
    # 完整序列也對得上 = 同一劇本的另一通（重撥）；否則只是第一輪像。
    if _sequence_match(case, call, directory):
        return f"重撥 #{ci} {case['id']}"
    return f"疑似 #{ci} {case['id']}（{note}）"


def compare(script: dict, directory, log_path: str | Path) -> int:
    calls = load_log(log_path)
    pairs, claimed = align_calls(script, calls, directory)
    _fill_positional_gaps(script, calls, pairs, claimed)

    rows: list[list[str]] = []
    drift: list[tuple[int, dict, LoggedCall, tuple, tuple, bool]] = []
    variant_notes: list[tuple[int, dict, str]] = []
    unrun: list[tuple[int, dict]] = []
    manual_pairs: list[tuple[int, dict, LoggedCall]] = []
    call_notes: dict[int, str] = {}
    auto_total = 0

    for ci, case in enumerate(script["calls"], start=1):
        expect_text = _display_expect(case["expect"])
        li = pairs.get(ci)
        if case["mode"] == "manual":
            rows.append([str(ci), case["id"], expect_text, "—", "manual 未執行"])
            if li is not None:
                manual_pairs.append((ci, case, calls[li]))
            continue
        auto_total += 1
        if li is None:
            rows.append([str(ci), case["id"], expect_text, "—", "未跑到"])
            unrun.append((ci, case))
            continue
        call = calls[li]
        got = _got_tuple(call)
        want = _want_tuple(case)
        actual = _actual_text(call, case)
        variant = _variant(case, call, ci, script, directory)
        if got == want:
            # 通過時只有「第一輪 0 候選」（最容易與 STT 崩壞混淆的那種）在表內標變體；
            # 其餘同音變體決策不變，列在表後的 STT 變體區，不干擾通過判讀。
            notes = []
            if variant and not _first_has_candidates(call, directory):
                notes.append("STT 變體")
            elif variant:
                variant_notes.append((ci, case, _variant_text(case, call)))
            note = f"（{'；'.join(notes)}）" if notes else ""
            rows.append(
                [str(ci), case["id"], expect_text, actual,
                 "通過" + note + _score_note(case, call)]
            )
        else:
            note = "（STT 變體）" if variant else ""
            drift.append((ci, case, call, want, got, variant))
            rows.append([str(ci), case["id"], expect_text, actual, "漂移" + note])

    # 沒對上任何劇本的 call（重撥、驗收雜音、STT 崩壞）：全部列出，不得靜默跳過。
    broken_calls: list[LoggedCall] = []
    unmatched_calls: list[LoggedCall] = []
    for li, call in enumerate(calls):
        if li in claimed:
            continue
        label = _extra_label(call, script, directory)
        call_notes[li] = label
        if label.startswith("STT 崩壞"):
            broken_calls.append(call)
        else:
            unmatched_calls.append(call)

    header = ["#", "情境", "預期", "實際", "判定"]
    widths = [
        max(_width(row[i]) for row in [header] + rows) for i in range(len(header))
    ]
    print(f"日誌：{log_path}（{len(calls)} 通 call）\n")
    print("  ".join(_pad(h, w) for h, w in zip(header, widths)).rstrip())
    for row in rows:
        print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())

    if drift:
        print("\n漂移（先看 action／reason，再看 target；docs/calls.md §3.1）：")
        for ci, case, call, want, got, variant in drift:
            print(
                f"  #{ci} {case['id']}：預期 {want[0]}/{want[1]}/{want[2] or '-'}"
                f"／實際 {got[0]}/{got[1]}/{got[2] or '-'}"
            )
            turns = " → ".join(_text(r) for r in call.turns) or "（空）"
            print(f"    逐字稿：{turns}（call_id {call.call_id}）")
            if variant:
                detail = _variant_text(case, call)
                if detail:
                    print(f"    STT 變體：{detail}")
            status = _stt_status(call)
            if status:
                print(f"    stt_status：{status}（先看重試路徑，不要拿它解釋決策漂移）")

    if variant_notes:
        print("\nSTT 變體（同音／近音；決策正確，照 expect 判通過）：")
        for ci, case, detail in variant_notes:
            print(f"  #{ci} {case['id']}：{detail}")

    if call_notes:
        print("\n未對上劇本的 call（含 STT 崩壞／重撥；不得靜默跳過）：")
        for li in sorted(call_notes):
            call = calls[li]
            print(
                f"  {call.call_id}：{call.first_text or '（空）'}"
                f" → {_actual_text(call)}"
            )
            print(f"    判定 {call_notes[li]}")

    if manual_pairs:
        print("\nmanual 對應的 call（照撥號順序；人工判定，docs/calls.md §3.5）：")
        for ci, case, call in manual_pairs:
            print(f"  #{ci} {case['id']}：{call.call_id}（{len(call.records)} 筆紀錄）")

    manual = sum(case["mode"] == "manual" for case in script["calls"])
    passed = sum(row[4].startswith("通過") for row in rows)
    print(
        f"\n{auto_total} 通 auto：{passed} 通過、{len(drift)} 漂移、"
        f"{len(unrun)} 未跑到、{len(unmatched_calls)} 對不上、"
        f"{len(broken_calls)} STT 崩壞"
    )
    print(f"{manual} 通 manual：未執行（人工判定，docs/calls.md §3.5）")
    print(
        f"日誌 {len(calls)} 通 call：{len(claimed)} 通對上劇本、"
        f"{len(calls) - len(claimed)} 通未對上"
    )
    return 1 if drift or unrun else 0



def _got_tuple(call: LoggedCall) -> tuple | None:
    final = call.final
    if final is None:
        return None
    d = _decision(final)
    return (d.get("action"), d.get("reason"), d.get("target") or None)


def _want_tuple(case: dict) -> tuple:
    expect = case["expect"]
    return (expect["action"], expect["reason"], expect["target"])


# --------------------------------------------------------------------------- #
# truth-out：日誌 → truth.csv（14 筆，依撥號順序）
# --------------------------------------------------------------------------- #
def truth_out(script: dict, directory, log_path: str | Path, out_path: str | Path) -> int:
    """依撥號順序產 truth.csv；`file` 取自該通第一輪的 wav 檔名。

    有序場次（測試場次專屬日誌）沿用 compare 的位置補位，STT 同音字也能對到；
    回溯日誌順序不可信，只認內容對上的通，缺任何一通就報錯、不寫半份檔。
    """
    calls = load_log(log_path)
    pairs, claimed = align_calls(script, calls, directory)
    _fill_positional_gaps(script, calls, pairs, claimed)

    rows = []
    missing = []
    for ci in TRUTH_ROWS:
        case = script["calls"][ci - 1]
        truth = case.get("truth")
        if not truth:
            raise SystemExit(f"#{ci} {case['id']} 沒有 truth 欄位，無法產生 truth.csv")
        li = pairs.get(ci)
        if li is None or not calls[li].first_wav:
            missing.append(f"#{ci} {case['id']}")
            continue
        rows.append(
            {
                "file": calls[li].first_wav,
                "truth_text": truth.get("truth_text", ""),
                "expect_pid": truth.get("expect_pid") or "",
                "kind": truth.get("kind", ""),
                "dept_expect": truth.get("dept_expect") or "",
            }
        )

    if missing:
        # 少寫一列而讓 eval.py 靜默少算，比報錯更糟；缺任何一通就不寫檔。
        print(
            f"無法產生 {len(TRUTH_ROWS)} 列的 truth.csv，缺這些通：",
            file=sys.stderr,
        )
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        return 1

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRUTH_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"寫入 {out}：{len(rows)} 列"
        "（撥號順序 #1–#10、#15–#18；file 取自日誌的 wav 檔名）"
    )
    return 0


# --------------------------------------------------------------------------- #
# stats：場次日誌的延遲與分佈（R3 上線量測；只讀 JSONL，不重跑決策）
# --------------------------------------------------------------------------- #
def _percentile(values: list[int], pct: int) -> int | None:
    """nearest-rank：排序後第 `ceil(pct/100 × n)` 個（不用插值，讀者不必換算）。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, (pct * len(ordered) + 99) // 100)
    return ordered[rank - 1]


def _count_line(label: str, counts: Counter) -> str:
    body = " ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "-"
    return f"{label}: {body}"


def stats(log_path: str | Path) -> int:
    """`stt.ms` 分佈、`decision.action`、`stream.status` 異常、`attempt`／`seq` 分佈。

    `stt.ms` 是 result 進門到拿到 transcript 的等待（`agi.py` 的量測點）。只收
    `stream.status` 為 `ok`／`dropped` 的輪次——WS 沒開（`error`／`stub`／`no-session`）
    的 0 不是延遲樣本；其餘列為 `skipped=`，搭配下一行的 `stream.status` 看。
    """
    calls = load_log(log_path)
    records = [record for call in calls for record in call.records]

    stt_ms: list[int] = []
    for record in records:
        ms = (record.get("stt") or {}).get("ms")
        status = (record.get("stream") or {}).get("status")
        if isinstance(ms, int) and status in ("ok", "dropped"):
            stt_ms.append(ms)
    skipped = len(records) - len(stt_ms)

    print(f"calls: {len(calls)}  turns: {len(records)}")
    if stt_ms:
        print(
            f"stt.ms: n={len(stt_ms)} p50={_percentile(stt_ms, 50)}"
            f" p95={_percentile(stt_ms, 95)} max={max(stt_ms)} skipped={skipped}"
        )
    else:
        print(f"stt.ms: n=0 p50=- p95=- max=- skipped={skipped}")

    action = Counter((r.get("decision") or {}).get("action") or "(missing)" for r in records)
    status = Counter((r.get("stream") or {}).get("status") or "(missing)" for r in records)
    attempt = Counter(str(r.get("attempt")) for r in records)
    seq = Counter(str(r.get("seq")) for r in records)

    print(_count_line("action", action))
    print(_count_line("stream.status", status))
    print(f"stream anomalies: {sum(n for s, n in status.items() if s != 'ok')}")
    print(_count_line("attempt", attempt))
    print(_count_line("seq", seq))
    return 0


# --------------------------------------------------------------------------- #
# replay（L2）：wav → STT → handle_turn；決策層與 STT 層分開判定
# --------------------------------------------------------------------------- #
# 節流：11-19 實撥 18 通內就撞到 429，所以每通之間預設停 2 秒，429 再退避重試。
REPLAY_PAUSE = 2.0
REPLAY_429_TRIES = 3
REPLAY_429_BACKOFF = 3.0
# Scribe 偶發回空字串（同一個 wav 手動重打就有字）：空逐字稿再試一次，
# 兩次都空才算這輪沒收到話（生產端由 dialplan 的 DBN_TRIES 重問）。
REPLAY_EMPTY_RETRIES = 1
# STT 層分類：規格要求的四類（人名同音／動詞殘骸／漏部門／贅詞）＋三種非變體情況。
VARIANT_KINDS = ("人名同音", "動詞殘骸", "漏部門", "贅詞", "劇本外", "空逐字稿", "無法分類")


class ReplayKeyError(RuntimeError):
    """沒有 ELEVENLABS_API_KEY：停下回報，不造假逐字稿。"""


def _load_env_file(path: Path | None = None) -> None:
    """把專案 `.env` 的 KEY=VALUE 補進 `os.environ`（已設定的值不覆蓋）。

    只有 replay 會呼叫：其他子命令不需要金鑰，也不該因為 `.env` 有副作用。
    `.env` 的格式是 systemd `EnvironmentFile`，沒有 `export`。
    路徑在呼叫時才讀 `ENV_PATH`，測試才能改成不存在的檔案驗「沒有 key」。
    """
    path = Path(path or ENV_PATH)
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def keyterms_from_directory(directory) -> list[str]:
    """名冊 → keyterms（姓名、別名、部門名、部門別名）；不手抄清單。

    與 `stt_batch.py` 同一組來源，但本工具不 import 它（AGENTS.md 契約 1）；
    保留繁體：Scribe 的 keyterms 只是提示，不影響 `core.normalize` 的簡→繁。
    """
    terms: list[str] = []
    for person in directory.people:
        terms.append(person.name)
        terms.extend(person.aliases)
    for dept in directory.departments:
        terms.append(dept.name)
        terms.extend(dept.aliases)
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        term = (term or "").strip()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


class _FailingStt:
    """重放失敗的 STT：讓 `handle_turn` 走它自己的 retry／stt-error 路徑。"""

    def __init__(self, error: BaseException):
        self.error = error

    def transcribe(self, path):
        raise self.error


def _resolve_wav(raw: str | Path, log_path: str | Path) -> Path:
    """日誌裡的 wav 是 PBX 絕對路徑；本機副本依序找場次目錄與 `recordings/`。"""
    path = Path(raw)
    if path.is_file():
        return path
    log = Path(log_path)
    roots = [log if log.is_dir() else log.parent, ROOT / "recordings"]
    for root in roots:
        for folder in (root, root / "recordings"):
            candidate = folder / path.name
            if candidate.is_file():
                return candidate
    return path


def _error_label(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status == 429:
        return f"429（退避 {REPLAY_429_TRIES} 次仍失敗）"
    if status:
        return f"HTTP {status}"
    return type(exc).__name__


def _transcribe(stt, wav, sleep):
    """一次轉稿：429 退避重試、空逐字稿再試一次。

    回傳 (text, error)；401/403 直接往上拋（key 問題）。其他錯誤不重試：
    重試一個格式錯誤或音檔損壞只是多花錢。
    """
    tries_429 = 0
    empty_left = REPLAY_EMPTY_RETRIES
    while True:
        try:
            text = (stt.transcribe(wav).text or "").strip()
        except SttAuthError:
            raise
        except SttError as exc:
            tries_429 += 1
            if getattr(exc, "status_code", None) != 429 or tries_429 >= REPLAY_429_TRIES:
                return "", exc
            sleep(REPLAY_429_BACKOFF * (2 ** (tries_429 - 1)))
            continue
        except Exception as exc:  # noqa: BLE001 — 連線錯誤等：記 stt-error，不中斷整場
            return "", exc
        if text or empty_left <= 0:
            return text, None
        empty_left -= 1
        sleep(REPLAY_429_BACKOFF)


def _stt_key(text: str) -> str:
    """STT 層的比對鍵：在 `core.normalize` 之上只多丟「非文字」字元。

    `core.PUNCT_RE` 不含 ASCII 句點，真實 Scribe 會吐「呃......你好」——那些點
    會卡在 `strip_noise` 的前綴比對前面，讓整句被誤判成人名同音。這裡只在
    **報表分類**這一層多丟一次標點，不動 `core.normalize`（決策層用的是原函式）。
    """
    return strip_noise("".join(ch for ch in normalize(text) if ch.isalnum()))[0]


def _dept_parts(text: str, directory) -> tuple[str | None, str]:
    """剝贅詞後找部門：回傳 (dept_id, 部門詞剝掉後剩下的字)。"""
    query = _stt_key(text)
    dept, rest = dept_match(query, directory.departments)
    return (dept.dept_id if dept else None), _stt_key(rest) if rest else ""


def classify_stt_variant(script_text: str, new_text: str, directory) -> str:
    """STT 層分類；正規化後字面相同回 ""（不算變體）。

    優先序（同一個差異只記一個主分類，避免統計重複）：
      贅詞     → 剝掉口語虛詞後字面相同（「呃」「麻煩一下」）
      漏部門   → 劇本有部門、逐字稿沒有，其餘部分同音（資訊部王偉 → 王偉）
      動詞殘骸 → 兩邊部門相同、逐字稿 0 候選且多出殘骸（幫我接業務部 → 報街業務部）
      人名同音 → 姓名片段發音相同，或兩邊都還找得到人（小美 → 小米 這種近音）
      無法分類 → 其餘（換句話說／答錯部門）
    `劇本外` 由呼叫端在劇本沒有對應輪次時給。
    """
    if normalize(script_text) == normalize(new_text):
        return ""
    if not normalize(new_text):
        return "空逐字稿"
    if _stt_key(script_text) == _stt_key(new_text):
        return "贅詞"

    want_dept, want_rest = _dept_parts(script_text, directory)
    got_dept, got_rest = _dept_parts(new_text, directory)

    if want_dept and not got_dept and want_rest:
        if _same_sound(want_rest, new_text) or _stt_key(want_rest) == _stt_key(new_text):
            return "漏部門"
    if want_dept and want_dept == got_dept and got_rest:
        got_pid, _ = _top1(new_text, directory)
        if got_pid is None and not _same_sound(want_rest, got_rest):
            return "動詞殘骸"

    want_pid, _ = _top1(script_text, directory)
    got_pid, _ = _top1(new_text, directory)
    if _same_sound(script_text, new_text) or (want_pid and got_pid):
        return "人名同音"
    return "無法分類"


@dataclass
class ReplayTurn:
    attempt: int
    wav: str
    script_text: str | None
    text: str
    variant: str
    error: str
    decision: dict


@dataclass
class ReplayCall:
    ci: int
    case: dict
    call_id: str
    turns: list[ReplayTurn]

    @property
    def final(self) -> dict:
        return self.turns[-1].decision if self.turns else {}

    @property
    def stt_errors(self) -> list[tuple[int, str]]:
        return [(t.attempt, t.error) for t in self.turns if t.error]


def _replay_call(ci: int, case: dict, call: LoggedCall, directory, stt,
                 log_path, sleep) -> ReplayCall:
    """依日誌輪序重播這通的 wav；候選 carry 一律用 `handle_turn` 的回傳值。"""
    prev = None
    turns: list[ReplayTurn] = []
    for index, record in enumerate(call.turns, start=1):
        wav = _resolve_wav(record.get("wav") or "", log_path)
        text, error = _transcribe(stt, wav, sleep)
        if error is None:
            result = handle_turn(wav, index, stt=FakeStt(text), directory=directory,
                                 prev_pids=prev)
            error_text = ""
        else:
            # 用失敗的 STT 重跑，決策才會跟生產的 retry／stt-error 一致。
            result = handle_turn(wav, index, stt=_FailingStt(error), directory=directory,
                                 prev_pids=prev)
            error_text = _error_label(error)
        script_text = case["turns"][index - 1] if index <= len(case["turns"]) else None
        if script_text is None:
            variant = "劇本外"
        elif error is not None:
            variant = ""
        else:
            variant = classify_stt_variant(script_text, text, directory)
        turns.append(ReplayTurn(index, str(wav), script_text, text, variant,
                                error_text, result))
        prev = result["cands"]
    return ReplayCall(ci, case, call.call_id, turns)


def _replay_row(replayed: ReplayCall) -> dict:
    case = replayed.case
    want = _want_tuple(case)
    final = replayed.final
    got = (final.get("action"), final.get("reason"), final.get("target") or None)
    if not replayed.turns:
        verdict = "未跑到"
    elif any(t.error for t in replayed.turns):
        # 有一輪的音檔沒轉成功，這通就沒有完整的逐字稿可比：記未判定，不拿
        # 後一輪的決策硬判通過（那會讓 stt-error 變成漂移的合法出口）。
        verdict = "未判定（stt-error）"
    elif got == want:
        verdict = "通過"
    elif len(replayed.turns) < len(case["turns"]) and got[0] == "ask":
        # 日誌只到第一輪（STT 出錯後就掛線），重播在追問就停了：決策層要到
        # 第二輪才有答案。記未判定，不能算漂移，也不能算通過。
        verdict = f"未判定（只到 {len(replayed.turns)}/{len(case['turns'])} 輪）"
    else:
        verdict = "漂移"
    return {
        "ci": replayed.ci,
        "id": case["id"],
        "call_id": replayed.call_id,
        "script": " → ".join(case["turns"]),
        "transcript": " → ".join(
            (f"（STT 失敗：{t.error}）" if t.error else (t.text or "（空逐字稿）"))
            for t in replayed.turns
        ),
        "expected": f"{want[0]}/{want[1]}/{want[2] or '-'}",
        "actual_full": f"{got[0]}/{got[1]}/{got[2] or '-'}",
        "actual": " → ".join(
            f"{t.decision.get('action')}"
            + (f" {t.decision['target']}" if t.decision.get("target") else
               (f" {t.decision['reason']}"
                if t.decision.get("action") in {"fallback", "hangup", "retry", "operator"}
                else ""))
            for t in replayed.turns
        ),
        "verdict": verdict,
        "turns": len(replayed.turns),
        "stt_same": sum(1 for t in replayed.turns if not t.variant and not t.error),
        "stt_failed": sum(1 for t in replayed.turns if t.error),
        "variants": [
            {"turn": t.attempt, "kind": t.variant, "detail": f"{t.script_text} → {t.text}"}
            for t in replayed.turns if t.variant
        ],
        "errors": [{"turn": t.attempt, "error": t.error} for t in replayed.turns if t.error],
    }


def _replay_summary(rows: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    for row in rows:
        for item in row["variants"]:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    return {
        "calls": len(rows),
        "consistent": sum(row["verdict"].startswith("通過") for row in rows),
        "drift": sum(row["verdict"] == "漂移" for row in rows),
        "undecided": sum(row["verdict"].startswith("未判定") for row in rows),
        "unrun": sum(row["verdict"] == "未跑到" for row in rows),
        "turns": sum(row["turns"] for row in rows),
        "stt_same": sum(row["stt_same"] for row in rows),
        "stt_failed": sum(row["stt_failed"] for row in rows),
        "variants": kinds,
    }


def _summary_line(summary: dict) -> str:
    return (
        f"{summary['calls']} 通 auto：決策一致 {summary['consistent']}、"
        f"漂移 {summary['drift']}、未判定 {summary['undecided']}、"
        f"未跑到 {summary['unrun']}"
    )


def _print_replay_report(title: str, rows: list[dict], summary: dict,
                         log_path, call_count: int, manual: int) -> None:
    print(f"\n== {title} ==")
    print(f"日誌：{log_path}（{call_count} 通 call；manual 不重播）\n")

    header = ["#", "情境", "劇本台詞", "新逐字稿", "決策", "判定"]
    body = [
        [str(row["ci"]), row["id"], row["script"], row["transcript"],
         row["actual"] or "—", row["verdict"]]
        for row in rows
    ]
    widths = [max(_width(row[i]) for row in [header] + body) for i in range(len(header))]
    print("  ".join(_pad(h, w) for h, w in zip(header, widths)).rstrip())
    for row in body:
        print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())

    # 決策層漂移：逐通列 expected／actual 與逐字稿，避免表格被寬欄位擠到看不出來。
    drifted = [row for row in rows if row["verdict"] == "漂移"]
    if drifted:
        print("\n漂移（決策層＝新決策 vs 劇本 expect）：")
        for row in drifted:
            print(f"  #{row['ci']} {row['id']}：預期 {row['expected']}／實際 {row['actual_full']}")
            print(f"    逐字稿：{row['transcript'] or '（空）'}（call_id {row['call_id']}）")

    undecided = [row for row in rows if row["verdict"].startswith("未判定")]
    if undecided:
        print("\n未判定（不是通過；逐通看得到原因）：")
        for row in undecided:
            print(f"  #{row['ci']} {row['id']}：{row['verdict']}（call_id {row['call_id']}）")

    # STT 層：分類／案例／次數／決策後果。
    agg: dict[str, dict] = {}
    for row in rows:
        for item in row["variants"]:
            entry = agg.setdefault(item["kind"], {"cases": [], "count": 0, "verdicts": {}})
            if row["ci"] not in entry["cases"]:
                entry["cases"].append(row["ci"])
            entry["count"] += 1
            entry["verdicts"][row["verdict"]] = entry["verdicts"].get(row["verdict"], 0) + 1
    if agg:
        print("\nSTT 錯誤分類（類型／案例／次數／決策後果；同音／近音照 expect 判，不當崩壞）：")
        table = [["類型", "案例", "次數", "決策後果"]]
        for kind in VARIANT_KINDS:
            entry = agg.get(kind)
            if not entry:
                continue
            cases = "、".join(f"#{ci}" for ci in entry["cases"])
            consequence = "、".join(f"{k} {v}" for k, v in entry["verdicts"].items())
            table.append([kind, cases, str(entry["count"]), consequence])
        widths = [max(_width(row[i]) for row in table) for i in range(4)]
        for row in table:
            print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())

    errors = [(row["ci"], row["id"], item) for row in rows for item in row["errors"]]
    if errors:
        print("\nSTT 呼叫失敗（該通記 stt-error，不中斷整場）：")
        for ci, case_id, item in errors:
            print(f"  #{ci} {case_id} 第 {item['turn']} 輪：{item['error']}")

    print()
    print(_summary_line(summary))
    print(
        f"STT 層 {summary['turns']} 輪：逐字稿相同 {summary['stt_same']}、"
        f"STT 變體 {sum(summary['variants'].values())}、"
        f"STT 失敗 {summary['stt_failed']}"
    )
    if summary["variants"]:
        detail = "、".join(
            f"{kind} {summary['variants'][kind]}"
            for kind in VARIANT_KINDS if kind in summary["variants"]
        )
        print(f"STT 變體分類：{detail}")
    if manual:
        print(f"{manual} 通 manual：跳過（不重播；沉默／按 0 由人工判定）")


def _print_ab(arms: list[dict]) -> None:
    if len(arms) != 2:
        return
    a, b = arms
    print("\n== A/B 對照（keyterms） ==")

    def rate(summary):
        return summary["consistent"] / summary["calls"] if summary["calls"] else 0.0

    table = [["指標", a["name"], b["name"]]]
    table.append([
        "人名同音錯聽次數",
        str(a["summary"]["variants"].get("人名同音", 0)),
        str(b["summary"]["variants"].get("人名同音", 0)),
    ])
    table.append([
        "決策一致",
        f"{a['summary']['consistent']}/{a['summary']['calls']}",
        f"{b['summary']['consistent']}/{b['summary']['calls']}",
    ])
    table.append(["決策一致率", f"{rate(a['summary']):.1%}", f"{rate(b['summary']):.1%}"])
    table.append([
        "未判定",
        str(a["summary"]["undecided"]),
        str(b["summary"]["undecided"]),
    ])
    table.append([
        "STT 失敗",
        str(a["summary"]["stt_failed"]),
        str(b["summary"]["stt_failed"]),
    ])
    widths = [max(_width(row[i]) for row in table) for i in range(3)]
    for row in table:
        print("  ".join(_pad(c, w) for c, w in zip(row, widths)).rstrip())


def _open_stt(factory, terms: list[str]):
    if factory is not None:
        return factory(terms)
    _load_env_file()
    client = SttClient(keyterms=terms)
    if not client.api_key:
        raise ReplayKeyError(
            "ELEVENLABS_API_KEY 未設定：請設環境變數或寫進專案根目錄的 .env"
            "（權限 600、不入版控）。沒有 key 不重播，也不造假逐字稿。"
        )
    return client


def _unrun_row(ci: int, case: dict) -> dict:
    want = _want_tuple(case)
    return {
        "ci": ci,
        "id": case["id"],
        "call_id": "",
        "script": " → ".join(case["turns"]),
        "transcript": "—",
        "expected": f"{want[0]}/{want[1]}/{want[2] or '-'}",
        "actual": "—",
        "actual_full": "—",
        "verdict": "未跑到",
        "turns": 0,
        "stt_same": 0,
        "stt_failed": 0,
        "variants": [],
        "errors": [],
    }


def replay(script: dict, directory, log_path: str | Path, *,
           keyterms: bool = False, ab: bool = False, stt_factory=None,
           pause: float = REPLAY_PAUSE, sleep=time.sleep,
           out_path: str | Path | None = None) -> int:
    """L2：場次 wav 重跑 STT → 決策 → 對照劇本 expect。

    `stt_factory` 讓測試注入假 STT（參數是該輪的 keyterms 清單）；
    不注入時建 `SttClient`，缺 key 直接 `ReplayKeyError`（不燒錢、不造假）。
    """
    calls = load_log(log_path)
    pairs, claimed = align_calls(script, calls, directory)
    _fill_positional_gaps(script, calls, pairs, claimed)

    terms = keyterms_from_directory(directory)
    if ab:
        arms = [("A（無 keyterms）", []), ("B（有 keyterms）", terms)]
    elif keyterms:
        arms = [("keyterms", terms)]
    else:
        arms = [("baseline", [])]
    # 先把所有 client 建好：--ab 時 B 的 key 問題不該等 A 跑完 18 次 API 才發現。
    clients = [(title, t, _open_stt(stt_factory, t)) for title, t in arms]

    auto_pairs = [
        (ci, script["calls"][ci - 1], calls[li])
        for ci, li in sorted(pairs.items())
        if script["calls"][ci - 1]["mode"] == "auto"
    ]
    paired = {ci for ci, _, _ in auto_pairs}
    manual = sum(case["mode"] == "manual" for case in script["calls"])
    total = len(auto_pairs) * len(clients)

    results = []
    slot = 0
    for title, terms_used, client in clients:
        rows = []
        for ci, case, call in auto_pairs:
            slot += 1
            print(f"[{slot}/{total}] #{ci} {case['id']}（{call.call_id}）…",
                  file=sys.stderr, flush=True)
            rows.append(_replay_row(
                _replay_call(ci, case, call, directory, client, log_path, sleep)
            ))
            if pause and slot < total:
                sleep(pause)
        for ci, case in enumerate(script["calls"], start=1):
            if case["mode"] == "auto" and ci not in paired:
                rows.append(_unrun_row(ci, case))
        rows.sort(key=lambda row: row["ci"])
        summary = _replay_summary(rows)
        _print_replay_report(title, rows, summary, log_path, len(calls), manual)
        results.append({
            "name": title,
            "keyterms": len(terms_used),
            "rows": rows,
            "summary": summary,
        })

    if len(results) == 2:
        _print_ab(results)

    session = Path(log_path).name if Path(log_path).is_dir() else Path(log_path).stem
    suffix = "-ab" if ab else ("-keyterms" if keyterms else "")
    out = Path(out_path) if out_path else REPLAY_DIR / f"replay-{session}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "log": str(log_path),
                "ab": ab,
                "arms": results,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"\n寫入 {out}")
    return 0 if all(arm["summary"]["consistent"] == arm["summary"]["calls"]
                    for arm in results) else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="calls.py",
        description="劇本回歸工具（L1）；協議與判準見 docs/calls.md",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("table", help="從 data/call_script.json 印 20 通撥號的 Markdown 表格")
    sub.add_parser("check", help="跑 18 通 auto＋3 個 l1_only；漂移則 exit 1")
    p_update = sub.add_parser(
        "update",
        help="刻意變更：重跑 L1，把 expect 搬到新行為並列 diff（台詞／truth 不動）",
    )
    p_update.add_argument("--dry-run", action="store_true", help="只印 diff，不寫檔")
    p_compare = sub.add_parser(
        "compare",
        help="測試場次日誌對劇本；通過／漂移／未跑到／對不上／STT 崩壞／manual",
    )
    p_compare.add_argument("log", help="AGI 日誌 .jsonl 或場次目錄（依檔名排序合併）")
    p_truth = sub.add_parser(
        "truth-out", help="依撥號順序從日誌產生 truth.csv（14 列）"
    )
    p_truth.add_argument("--calls", required=True, help="AGI 日誌 .jsonl 或場次目錄")
    p_truth.add_argument(
        "--out",
        default=str(TRUTH_PATH),
        help=f"輸出的 truth.csv（預設 {TRUTH_PATH.relative_to(ROOT)}）",
    )
    p_stats = sub.add_parser(
        "stats", help="場次 JSONL：stt.ms 分佈、action／stream.status／attempt／seq 統計"
    )
    p_stats.add_argument("log", help="AGI 日誌 .jsonl 或場次目錄（依檔名排序合併）")
    p_replay = sub.add_parser(
        "replay", help="L2：場次 wav 重跑 STT → 決策；需 ELEVENLABS_API_KEY"
    )
    p_replay.add_argument("log", help="AGI 日誌 .jsonl 或場次目錄")
    p_replay.add_argument(
        "--keyterms", action="store_true",
        help="把名冊姓名／別名／部門詞當 keyterms 提示傳給 STT",
    )
    p_replay.add_argument(
        "--ab", action="store_true",
        help="無／有 keyterms 各跑一輪並列對照（API 次數加倍）",
    )
    p_replay.add_argument(
        "--pause", type=float, default=REPLAY_PAUSE,
        help=f"每通之間暫停秒數（預設 {REPLAY_PAUSE}；429 會再退避重試）",
    )
    p_replay.add_argument("--out", help="報告 JSON（預設 out/replay-<場次>.json）")
    args = ap.parse_args(argv)

    script = load_script()
    if args.cmd == "table":
        print(format_table(script))
        return 0

    directory = load_directory(DATA_PATH)
    if args.cmd == "check":
        return check(script, directory)
    if args.cmd == "update":
        return update(script, directory, dry_run=args.dry_run)
    if args.cmd == "compare":
        return compare(script, directory, args.log)
    if args.cmd == "stats":
        return stats(args.log)
    if args.cmd == "replay":
        try:
            return replay(
                script, directory, args.log,
                keyterms=args.keyterms, ab=args.ab,
                pause=args.pause, out_path=args.out,
            )
        except ReplayKeyError as exc:
            print(exc, file=sys.stderr)
            return 2
        except SttAuthError as exc:
            print(f"STT 認證失敗，中止重播：{exc}", file=sys.stderr)
            return 2
    return truth_out(script, directory, args.calls, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
