# SPDX-License-Identifier: AGPL-3.0-or-later
"""handle_turn：STT → decide → DBN_* 的六個變數與失敗對應。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dialname.agi import handle_turn
from dialname.core import load_directory
from dialname.stt_client import SttAuthError

ROSTER = {
    "source_directory": "PBX Internal",
    "departments": [
        {"dept_id": "sales", "name": "業務部", "aliases": ["業務", "Sales"]},
        {"dept_id": "cs", "name": "客服部", "aliases": ["客服", "CS"]},
    ],
    "people": [
        {
            "pid": "u001", "name": "王偉", "directory": "PBX Internal",
            "dept": "業務部", "title": "業務經理", "extension": "2101",
            "last_name": "王", "first_name": "偉",
        },
        {
            "pid": "u002", "name": "林小華", "directory": "PBX Internal",
            "dept": "客服部", "title": "客服專員", "extension": "2201",
            "last_name": "林", "first_name": "小華",
        },
    ],
}


class FakeStt:
    def __init__(self, text="", exc=None):
        self.text = text
        self.exc = exc

    def transcribe(self, path):
        if self.exc:
            raise self.exc
        return SimpleNamespace(text=self.text, language="zho")


@pytest.fixture()
def directory(tmp_path):
    path = tmp_path / "people.json"
    path.write_text(json.dumps(ROSTER, ensure_ascii=False), encoding="utf-8")
    return load_directory(path)


def _turn(directory, tmp_path, stt):
    return handle_turn(
        "call.wav", 1, stt=stt, directory=directory,
        log_dir=tmp_path / "logs", call_id="123.45",
    )


def test_normal_route_and_ascii(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt("王偉"))
    assert result["action"] == "route"
    assert result["target"] == "2101"
    assert result["speech"] == ["transferring"]
    for value in result.values():
        if isinstance(value, str):
            assert value.isascii()
        if isinstance(value, list):
            assert all(v.isascii() for v in value)


def test_bare_department_routes(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt("業務部"))
    assert result["action"] == "route_dept"
    assert result["dept"] == "sales"
    assert result["speech"] == ["transferring", "dept-sales"]


def test_empty_transcript_retries(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt(""))
    assert result["action"] == "retry"
    assert result["reason"] == "stt-empty"
    assert result["speech"] == []


def test_empty_transcript_retries_on_second_attempt(directory, tmp_path):
    result = handle_turn(
        "call.wav", 2, stt=FakeStt(""), directory=directory,
        log_dir=tmp_path / "logs", call_id="123.46",
    )
    assert result["action"] == "retry"


def test_no_candidate_giveup_falls_back(directory, tmp_path):
    result = handle_turn(
        "call.wav", 2, stt=FakeStt("我要找一個不存在的人"), directory=directory,
        log_dir=tmp_path / "logs", call_id="123.47",
    )
    assert result["action"] == "fallback"
    assert result["reason"] == "no-candidate-giveup"


def test_result_always_has_six_keys(directory, tmp_path):
    for stt in (FakeStt("王偉"), FakeStt(""), FakeStt(exc=SttAuthError("x", 401))):
        result = _turn(directory, tmp_path, stt)
        assert set(result) == {"action", "target", "dept", "speech", "reason", "cands"}


def test_auth_error_hangs_up_with_goodbye(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt(exc=SttAuthError("bad key", 401)))
    assert result["action"] == "hangup"
    assert result["speech"] == ["goodbye"]


def test_timeout_retries(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt(exc=TimeoutError("slow")))
    assert result["action"] == "retry"
    assert result["reason"] == "stt-error"


def test_log_is_one_line_per_turn(directory, tmp_path):
    _turn(directory, tmp_path, FakeStt("王偉"))
    files = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["call_id"] == "123.45"
    assert rows[0]["attempt"] == 1
    assert rows[0]["decision"]["action"] == "route"


def test_second_turn_department_narrows_previous_candidates(directory, tmp_path):
    """第 2 輪只講部門 → 用 DBN_CANDS 把上一輪候選收斂到該部門。

    2026-11-14 實機：第 1 輪說出姓名得到 2 個候選（業務／客服各一），
    第 2 輪「客服部。」原本因為沒有候選而轉整個部門；收斂後應直接轉客服部那個人。
    """
    result = handle_turn(
        "call.wav", 2, stt=FakeStt("客服部"), directory=directory,
        log_dir=tmp_path / "logs", call_id="123.48",
        prev_pids="u001:0.897&u002:0.822",
    )
    assert result["action"] == "route"
    assert result["target"] == "2201"        # u002 林小華（客服部）
    assert result["reason"] == "single-candidate"
    assert result["cands"] == "u002:0.822"


def test_second_turn_department_without_intersection_falls_back(directory, tmp_path):
    """第 2 輪答了沒有交集的部門 → 不可默默轉那個部門。

    實機情境：找「客服部的陳美玲」，第 2 輪答「人事部」——來電者從沒說要轉人事部，
    那句話是澄清答案。收斂不到人應該交總機，而不是 `route_dept`。
    """
    result = handle_turn(
        "call.wav", 2, stt=FakeStt("業務部"), directory=directory,
        log_dir=tmp_path / "logs", call_id="123.49",
        prev_pids="u002:0.900",              # 上一輪只有客服部的人，與「業務部」無交集
    )
    assert result["action"] == "fallback"
    assert result["reason"] == "no-candidate-giveup"
    assert result["dept"] == ""
    assert result["speech"] == ["not-found"]   # 先說明「無法確認」，再轉總機

    rows = [json.loads(line) for f in (tmp_path / "logs").glob("*.jsonl")
            for line in f.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["dept_ignored"] == "sales"


def test_first_turn_exposes_candidates_for_next_turn(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt("王偉"))
    assert result["action"] == "route"
    assert result["cands"] == "u001:1.000"


def test_retry_keeps_previous_candidates(directory, tmp_path):
    """空逐字稿 retry 時不得把上一輪候選清掉（下一輪還要收斂）。"""
    result = handle_turn(
        "call.wav", 2, stt=FakeStt(""), directory=directory,
        log_dir=tmp_path / "logs", call_id="123.50",
        prev_pids="u001:0.900&u002:0.800",
    )
    assert result["action"] == "retry"
    assert result["cands"] == "u001:0.900&u002:0.800"


def test_retroflex_department_answer_still_converges(directory, tmp_path):
    """部門名被聽成同音字（「客服部」→「客扶部」）時，仍要收斂到上一輪的人。"""
    result = handle_turn(
        "call.wav", 2, stt=FakeStt("客扶部"), directory=directory,
        log_dir=tmp_path / "logs", call_id="123.51",
        prev_pids="u002:0.900",
    )
    assert result["action"] == "route"
    assert result["target"] == "2201"       # u002 林小華（客服部）


def test_unknown_name_with_roster_surname_retries_instead_of_hanging_up(directory, tmp_path):
    """名冊查不到、但首字是名冊姓氏 → 要重問一次，不能當成垃圾句掛線。"""
    result = _turn(directory, tmp_path, FakeStt("林大明"))
    assert result["action"] == "ask"
    assert result["reason"] == "no-candidate-retry"


def test_garbage_without_surname_still_hangs_up(directory, tmp_path):
    result = _turn(directory, tmp_path, FakeStt("你們便當訂了沒"))
    assert result["action"] == "hangup"
    assert result["reason"] == "no-intent"
