# SPDX-License-Identifier: AGPL-3.0-or-later
"""L1：劇本決策層回歸（20 通撥號劇本＋3 個 `l1_only` 案例）。

資料單一來源是 `data/call_script.json`（`docs/design.md` §回歸三層）。
人工撥號協議與通過判準見 `docs/calls.md`；電話層驗收、重試與掛線的
分工見 `docs/dialplan.md` §4（§3.9 說明 `retry` 與 `DBN_TRIES` 掛線）。

每個 `auto` 案例照真實順序呼叫 `handle_turn`：第二輪把上一輪回傳的 `cands`
原樣餵回 `prev_pids`，測真實的候選 carry，不寫死候選。斷言只比最後一輪的
`action`／`reason`／`target`；第一輪 top1 是警告層（計畫 §7）：

* 分數差 > 0.10 → fail
* 分數差 0.02–0.10 → warning

`mode: manual` 的案例（沉默、按 0）不跑，只斷言 id 集合，避免有人默默刪掉手動項。
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from dialname.agi import CARRY_SEP, handle_turn
from dialname.core import load_directory

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "data" / "call_script.json"
WAV = "/tmp/does-not-exist.wav"          # 只需讓 _wav_duration_ms() 回 None

SCRIPT = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
CALLS = SCRIPT["calls"]
AUTO_CASES = [case for case in CALLS if case["mode"] == "auto"] + SCRIPT["l1_only"]
EXPECTED_MANUAL_IDS = {"silence", "dtmf-operator"}


class FakeStt:
    """只回固定逐字稿；不碰音檔、不碰網路。"""

    def __init__(self, text: str = ""):
        self.text = text

    def transcribe(self, path):
        return SimpleNamespace(text=self.text, language="zho")


@pytest.fixture(scope="module")
def directory():
    return load_directory(ROOT / "data" / "people.json")


def _run_turns(case: dict, directory):
    """依序跑完案例；回傳（最後一輪, 第一輪, 最後一輪收到的 prev_pids）。"""
    prev = None
    first = None
    last_prev = None
    result = None
    for attempt, text in enumerate(case["turns"], start=1):
        last_prev = prev
        result = handle_turn(
            WAV, attempt,
            stt=FakeStt(text),
            directory=directory,
            prev_pids=prev,
        )
        if attempt == 1:
            first = result
        prev = result["cands"]
    return result, first, last_prev


def _top1_score(cands: str) -> float | None:
    """`pid:score&pid:score` → 第一個（最高分）分數；沒有候選回 None。"""
    if not cands:
        return None
    pid_score = cands.split(CARRY_SEP, 1)[0]
    return float(pid_score.rpartition(":")[2])


@pytest.mark.parametrize("case", AUTO_CASES, ids=[case["id"] for case in AUTO_CASES])
def test_scripted_call(case, directory):
    assert case["turns"], f"{case['id']} 是 auto 案例，卻沒有逐字稿"

    result, first, last_prev = _run_turns(case, directory)
    expect = case["expect"]

    got = (result["action"], result["reason"], result["target"] or None)
    want = (expect["action"], expect["reason"], expect["target"])
    assert got == want, (
        f"{case['id']}（{case['kind']}）最後一輪決策漂移：\n"
        f"  預期 {want}\n"
        f"  實際 {got}\n"
        f"  逐字稿 {case['turns']}"
    )

    # agi.py 的 stt_status="empty" 分支：retry 不得清掉上一輪候選，下一輪還要收斂。
    if result["action"] == "retry":
        assert result["cands"] == (last_prev or ""), (
            f"{case['id']}：retry 把上一輪候選弄丟了 "
            f"（prev={last_prev!r}, now={result['cands']!r}）"
        )

    # 分數層只警告，不當硬失敗；差太多才擋。
    want_scores = expect.get("scores") or []
    got_score = _top1_score(first["cands"])
    if not want_scores:
        assert got_score is None, (
            f"{case['id']}：第一輪預期 0 候選，實際 top1={got_score:.3f}"
        )
    else:
        assert got_score is not None, (
            f"{case['id']}：第一輪預期 top1={want_scores[0]:.3f}，實際 0 候選"
        )
        delta = abs(round(got_score, 3) - want_scores[0])
        if delta > 0.10:
            pytest.fail(
                f"{case['id']}：第一輪 top1 差 {delta:.3f}（> 0.10）"
                f"，預期 {want_scores[0]:.3f}、實際 {got_score:.3f}"
            )
        if delta >= 0.02:
            warnings.warn(
                f"{case['id']}：第一輪 top1 位移 {delta:.3f}"
                f"（預期 {want_scores[0]:.3f}、實際 {got_score:.3f}）",
                stacklevel=2,
            )


def test_manual_case_ids():
    """manual 案例不跑；id 集合被動到就紅燈（計畫 §3）。"""
    manual = [case for case in CALLS if case["mode"] == "manual"]
    assert {case["id"] for case in manual} == EXPECTED_MANUAL_IDS
    for case in manual:
        assert case["truth"] is None
        assert case["turns"] == []
        assert set(case["expect"]) >= {"action", "reason", "target"}


def test_script_shape():
    """20 通撥號劇本＋3 個 l1_only；刪掉案例不能只是少一個測試。"""
    assert SCRIPT["version"] == 1
    ids = [case["id"] for case in CALLS]
    assert len(ids) == 20
    assert len(set(ids)) == len(ids)
    assert sum(case["mode"] == "auto" for case in CALLS) == 18
    assert len(SCRIPT["l1_only"]) == 3
    assert all(case["mode"] == "auto" for case in SCRIPT["l1_only"])


def test_calls_md_table_in_sync():
    """`docs/calls.md` 的 20 通表格必須是 `calls.py table` 的輸出（表格 guard）。"""
    output = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "calls.py"), "table"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    calls_md = (ROOT / "docs" / "calls.md").read_text(encoding="utf-8")
    assert output in calls_md, (
        "docs/calls.md 的表格與 data/call_script.json 漂移；"
        "跑 `uv run scripts/calls.py table` 重貼輸出（勿手改）"
    )
