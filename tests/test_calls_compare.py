# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/calls.py compare`／`truth-out`：場次日誌的機器判定。

對齊方式與判定規則見 `docs/design.md` §回歸三層 與 `docs/calls.md` §3：
逐字稿先過 `dialname.core` 正規化（含簡→繁）；文字不同但拼音相同（含 core 的模糊音）
視為同句並標「STT 變體」，不丟進 STT 崩壞；錨點之間才用撥號順序補位。
STT 崩壞用可操作定義：正規化後 0 候選、部門解析失敗，**且**拼音對不上任何劇本台詞、
位置也補不上。這裡用**構造的 JSONL** 驗證分類，不重跑決策層（那由
`test_call_script.py` 負責）。

分類：通過／漂移／未跑到／對不上／STT 崩壞，另加 manual 未執行；
對不上的 call 必須列在報表裡，不得靜默跳過（`eval.py --calls` 的 `[skip]` 教訓）。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dialname.core import load_directory, normalize

ROOT = Path(__file__).resolve().parents[1]
CALLS_PY = ROOT / "scripts" / "calls.py"
SCRIPT = json.loads((ROOT / "data" / "call_script.json").read_text(encoding="utf-8"))
AUTO = [
    (index, case)
    for index, case in enumerate(SCRIPT["calls"], start=1)
    if case["mode"] == "auto"
]


@pytest.fixture(scope="module")
def directory():
    return load_directory(ROOT / "data" / "people.json")


def _record(call_id: str, turn: int, text: str, decision: dict, directory) -> dict:
    cands, _dept, _q, _meta = directory.search_meta(normalize(text))
    return {
        "ts": f"2026-11-19T10:{turn:02d}:00+08:00",
        "call_id": call_id,
        "attempt": turn,
        "wav": f"/var/lib/asterisk/dial-by-name/recordings/{call_id}-{turn}.wav",
        "wav_duration_ms": 3000,
        "stt": {"text": text, "lang": "zho", "ms": 800},
        "candidates": [
            {"pid": c.person.pid, "score": round(c.score, 4)} for c in cands[:5]
        ],
        "decision": decision,
        "stt_status": "ok",
        "decide_ms": 1,
    }


def _case_records(case: dict, call_id: str, directory) -> list[dict]:
    """依劇本輪次造紀錄；最後一輪用 expect 當決策，前面是澄清 ask。"""
    records = []
    for turn, text in enumerate(case["turns"], start=1):
        last = turn == len(case["turns"])
        expect = case["expect"]
        decision = {
            "action": expect["action"] if last else "ask",
            "target": expect["target"] if last else None,
            "reason": expect["reason"] if last else "clarify",
            "confidence": 0.9,
            "speech": [],
        }
        records.append(_record(call_id, turn, text, decision, directory))
    return records


def build_lines(directory, *, drop: set[int] = frozenset(),
                broken: set[int] = frozenset(),
                overrides: dict[int, dict] | None = None) -> list[dict]:
    """18 通 auto 的完整有序日誌；drop＝少撥、broken＝逐字稿全毀、overrides＝改決策。"""
    lines: list[dict] = []
    for index, case in AUTO:
        if index in drop:
            continue
        call_id = f"test-{index:02d}"
        if index in broken:
            lines.append(
                _record(
                    call_id, 1, "啊啊啊",
                    {"action": "hangup", "target": None, "reason": "no-intent",
                     "confidence": None, "speech": []},
                    directory,
                )
            )
            continue
        records = _case_records(case, call_id, directory)
        if overrides and index in overrides:
            records[-1]["decision"] = overrides[index]
        lines.extend(records)
    return lines


def _replace_call(lines: list[dict], call_id: str, record: dict) -> list[dict]:
    """把一通 call 的所有紀錄換成單筆（保留原本的撥號順序）。"""
    out: list[dict] = []
    placed = False
    for row in lines:
        if row["call_id"] == call_id:
            if not placed:
                out.append(record)
                placed = True
            continue
        out.append(row)
    return out


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in lines),
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CALLS_PY), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_compare_all_pass(tmp_path, directory):
    log = _write_jsonl(tmp_path / "20261119.jsonl", build_lines(directory))
    result = _run("compare", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 通 auto：18 通過、0 漂移、0 未跑到、0 對不上、0 STT 崩壞" in result.stdout
    # manual 不計入通過率，但必須顯示「未執行」而不是消失。
    assert "manual 未執行" in result.stdout
    # 每通 call 只能被認領一次：通過的通不得又出現在未對上清單（#9 重複計算的 bug）。
    assert "未對上劇本的 call" not in result.stdout
    assert "18 通對上劇本、0 通未對上" in result.stdout


def test_compare_drift(tmp_path, directory):
    log = _write_jsonl(
        tmp_path / "20261119.jsonl",
        build_lines(
            directory,
            overrides={
                9: {"action": "route", "target": "2202", "reason": "direct:confident",
                    "confidence": 0.9, "speech": []}
            },
        ),
    )
    result = _run("compare", str(log))

    assert result.returncode == 1
    assert "18 通 auto：17 通過、1 漂移、0 未跑到" in result.stdout
    assert "#9 filler-sc：預期 route/direct:confident/2101" in result.stdout
    assert "實際 route/direct:confident/2202" in result.stdout


def test_compare_homophone_variant_uses_content_anchor(tmp_path, directory):
    """真實 STT 把「業務部的汪偉」寫成「業務部王偉」：拼音相同＝同句，不是漂移。"""
    lines = build_lines(directory)
    record = _record(
        "test-03", 1, "業務部王偉。",
        {"action": "route", "target": "2101", "reason": "direct:confident",
         "confidence": 0.9, "speech": []},
        directory,
    )
    log = _write_jsonl(tmp_path / "20261119.jsonl", _replace_call(lines, "test-03", record))
    result = _run("compare", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 通 auto：18 通過、0 漂移" in result.stdout
    assert "homophone-dept-sc" in result.stdout
    assert "通過（分數 0.947→1.000）" in result.stdout
    # 決策不變的同音變體不佔用 STT 崩壞，列在表後留紀錄。
    assert "STT 變體（同音／近音" in result.stdout
    assert "業務部的汪偉 → 業務部王偉。" in result.stdout


def test_compare_variant_without_candidates_is_not_broken(tmp_path, directory):
    """美林是合法的 given-name-only（0 候選本來就正確），不得記成 STT 崩壞。"""
    lines = build_lines(directory)
    record = _record(
        "test-06", 1, "美林。",
        {"action": "fallback", "target": None, "reason": "alias-not-found",
         "confidence": None, "speech": []},
        directory,
    )
    log = _write_jsonl(tmp_path / "20261119.jsonl", _replace_call(lines, "test-06", record))
    result = _run("compare", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 通 auto：18 通過、0 漂移、0 未跑到、0 對不上、0 STT 崩壞" in result.stdout
    assert "通過（STT 變體）" in result.stdout
    assert "判定 STT 崩壞" not in result.stdout


def test_compare_duplicate_transcript_is_distinct_cases(tmp_path, directory):
    """小米×3 是 #12/#13/#14 三個 case（#12/#13 的「小美」被聽成小米），不是重撥。"""
    lines = build_lines(directory)
    decision = {"action": "fallback", "target": None,
                "reason": "low-confidence-single", "confidence": None, "speech": []}
    for call_id in ("test-12", "test-13"):
        lines = _replace_call(
            lines, call_id, _record(call_id, 1, "小米。", decision, directory)
        )
    log = _write_jsonl(tmp_path / "20261119.jsonl", lines)
    result = _run("compare", str(log))

    assert result.returncode == 1
    assert "18 通 auto：16 通過、2 漂移、0 未跑到、0 對不上、0 STT 崩壞" in result.stdout
    # 同逐字稿的三通不得一律當成 #14 的重撥。
    assert "重撥 #14" not in result.stdout
    assert "#12 alias-homophone-cs：預期 route/direct:confident/2201" in result.stdout
    assert "#13 alias-homophone-adm：預期 route/direct:confident/6101" in result.stdout
    assert "STT 變體：小美 → 小米。" in result.stdout
    # #14 用位置挑到最後一通（撥號順序），照樣通過。
    assert "14  alias-floor" in result.stdout
    assert "通過" in result.stdout


def test_compare_stt_broken_is_listed_not_silent(tmp_path, directory):
    """位置也補不上（前後錨點間數量不等）才算崩壞；call 必須逐通列出來。"""
    log = _write_jsonl(
        tmp_path / "20261119.jsonl", build_lines(directory, drop={9}, broken={8})
    )
    result = _run("compare", str(log))

    assert result.returncode == 1
    assert "18 通 auto：16 通過、0 漂移、2 未跑到、0 對不上、1 STT 崩壞" in result.stdout
    assert "8   dept-surname-sc" in result.stdout
    assert "test-08：啊啊啊" in result.stdout
    assert "判定 STT 崩壞" in result.stdout


def test_compare_redial_keeps_broken_call_visible(tmp_path, directory):
    """崩壞後重撥成功：劇本算通過，但崩壞那通仍要單獨列出來。"""
    lines = build_lines(directory, broken={8})
    pos = next(i for i, row in enumerate(lines) if row["call_id"] == "test-08")
    lines[pos + 1:pos + 1] = _case_records(AUTO[7][1], "test-08-retry", directory)
    log = _write_jsonl(tmp_path / "20261119.jsonl", lines)
    result = _run("compare", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 通 auto：18 通過、0 漂移、0 未跑到、0 對不上、1 STT 崩壞" in result.stdout
    assert "test-08：啊啊啊" in result.stdout
    assert "判定 STT 崩壞" in result.stdout


def test_compare_retry_turn_is_not_a_script_turn(tmp_path, directory):
    """429 retry 輪（空逐字稿、action=retry）不計入劇本輪。"""
    lines = build_lines(directory)
    retry = _record(
        "test-16", 1, "",
        {"action": "retry", "target": None, "reason": "stt-error",
         "confidence": None, "speech": []},
        directory,
    )
    retry["ts"] = "2026-11-19T09:00:00+08:00"
    retry["stt_status"] = "429"
    retry["wav"] = "/var/lib/asterisk/dial-by-name/recordings/test-16-0.wav"
    pos = next(i for i, row in enumerate(lines) if row["call_id"] == "test-16")
    lines.insert(pos, retry)
    log = _write_jsonl(tmp_path / "20261119.jsonl", lines)
    result = _run("compare", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 通 auto：18 通過、0 漂移、0 未跑到" in result.stdout
    assert "多出" not in result.stdout


def test_compare_unrun(tmp_path, directory):
    log = _write_jsonl(tmp_path / "20261119.jsonl", build_lines(directory, drop={4}))
    result = _run("compare", str(log))

    assert result.returncode == 1
    assert "17 通過、0 漂移、1 未跑到" in result.stdout
    assert "4   drop-char-sc" in result.stdout
    assert "未跑到" in result.stdout


def test_compare_directory_merges_midnight_files(tmp_path, directory):
    """跨午夜散兩檔：目錄依檔名排序合併，同一個 call 的兩輪也要接回來。"""
    lines = build_lines(directory)
    # 刻意切在 call 中間：第二輪在第二個檔，靠 call_id 重組。
    split = len(lines) // 2
    _write_jsonl(tmp_path / "session" / "20261118.jsonl", lines[:split])
    _write_jsonl(tmp_path / "session" / "20261119.jsonl", lines[split:])

    result = _run("compare", str(tmp_path / "session"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "18 通 auto：18 通過" in result.stdout
    assert "18 通 call" in result.stdout


def test_compare_lists_unmatched_calls(tmp_path, directory):
    """對不上劇本的 call（有候選但不是任何一題）要列出來，不得靜默跳過。"""
    lines = build_lines(directory)
    extra = _record(
        "stray-001", 1, "客服部的王偉",
        {"action": "route", "target": "2202", "reason": "single-candidate",
         "confidence": 0.9, "speech": []},
        directory,
    )
    log = _write_jsonl(tmp_path / "20261119.jsonl", lines + [extra])
    result = _run("compare", str(log))

    assert "stray-001：客服部的王偉" in result.stdout
    assert "判定 對不上" in result.stdout
    assert "1 對不上、0 STT 崩壞" in result.stdout
    assert "1 通未對上" in result.stdout


def test_truth_out_rows_and_order(tmp_path, directory):
    log = _write_jsonl(tmp_path / "20261119.jsonl", build_lines(directory))
    out = tmp_path / "truth.csv"
    result = _run("truth-out", "--calls", str(log), "--out", str(out))

    assert result.returncode == 0, result.stdout + result.stderr
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0] == ["file", "truth_text", "expect_pid", "kind", "dept_expect"]
    assert len(rows) == 15                     # 表頭＋14 筆

    body = rows[1:]
    expected = [
        ("我要找鄭博文", "u016", "exact-sc", ""),
        ("我要找汪偉", "u002", "homophone-sc", "客服部"),
        ("業務部的汪偉", "u001", "homophone-dept-sc", "業務部"),
        ("張豪", "u004", "drop-char-sc", ""),
        ("王先生", "", "surname-only-sc", ""),
        ("美玲", "", "given-name-only-sc", ""),
        ("幫我接業務部", "", "dept-only-sc", "業務部"),
        ("業務部的王先生", "", "dept+surname-sc", "業務部"),
        ("你好我要找業務部的王偉麻煩一下", "u001", "filler-sc", ""),
        ("你們便當訂了沒", "", "garbage", ""),
        ("我要找李佳蓉", "u010", "same-name-homophone-sc", ""),
        ("資訊部王偉", "u001,u002", "homophone-sc", ""),
        ("我要找古廷瑋", "u040", "single-candidate-floor-sc", ""),
        ("我要找陳美玲", "u008", "dept-answer-wrong-sc", ""),
    ]
    assert [tuple(row[1:]) for row in body] == expected
    # file 取自日誌第一輪的 wav；兩輪案例也取第一輪（名稱那句）。
    assert body[0][0] == "test-01-1.wav"
    assert body[1][0] == "test-02-1.wav"
    assert body[11][0] == "test-16-1.wav"


def test_truth_out_refuses_partial_file(tmp_path, directory):
    log = _write_jsonl(tmp_path / "20261119.jsonl", build_lines(directory, drop={18}))
    out = tmp_path / "truth.csv"
    result = _run("truth-out", "--calls", str(log), "--out", str(out))

    assert result.returncode == 1
    assert "#18 dept-answer-wrong-sc" in result.stderr
    assert not out.exists(), "缺通時不得寫出少於 14 列的 truth.csv"
