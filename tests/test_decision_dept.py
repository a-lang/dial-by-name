# SPDX-License-Identifier: AGPL-3.0-or-later
"""決策層：dept_id 路由、hangup 政策、第二輪規則。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dialname.core import Candidate, load_directory
from dialname.decision import decide


ROSTER = {
    "source_directory": "PBX Internal",
    "departments": [
        {"dept_id": "sales", "name": "業務部", "aliases": ["業務", "Sales"]},
        {"dept_id": "cs", "name": "客服部", "aliases": ["客服", "CS"]},
        {"dept_id": "it", "name": "資訊部", "aliases": ["資訊", "IT"]},
        {"dept_id": "rd", "name": "工程部", "aliases": ["工程", "RD"]},
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


@pytest.fixture()
def directory(tmp_path):
    path = tmp_path / "people.json"
    path.write_text(json.dumps(ROSTER, ensure_ascii=False), encoding="utf-8")
    return load_directory(path)


def test_route_dept_uses_dept_id_and_two_clips(directory):
    cands, dept, q = directory.search("業務部")
    dec = decide(["業務部"], cands, dept, attempt=1,
                 departments=directory.departments, dept_residual=q)
    assert dec.action == "route_dept"
    assert dec.target == "sales"
    assert dec.speech == ["sounds/zh/transferring", "sounds/zh/dept-sales"]


# ── 部門限定的第二輪追問（2026-11-19 方案 C，實撥 #7「报街业务部」） ──────


def test_dept_resolved_garbage_asks_department(directory):
    """部門有解析到＋0 候選 → 問部門，不掛線（`no-intent` 只在無部門時成立）。"""
    cands, dept, q = directory.search("我要談業務合作")
    assert cands == [] and dept is not None
    dec = decide(["我要談業務合作"], cands, dept, attempt=1,
                 departments=directory.departments, dept_residual=q)
    assert dec.action == "ask"
    assert dec.reason == "dept-scoped-ask"
    # 先把部門念回去，再問要找哪一位
    assert dec.speech == ["sounds/zh/dept-sales", "sounds/zh/q-dept-who"]


def test_pure_garbage_without_department_still_hangs_up(directory):
    """無部門的純亂句行為完全不變：第一輪掛線，不浪費第二輪。"""
    cands, dept, q = directory.search("我要訂便當")
    assert cands == [] and dept is None
    dec = decide(["我要訂便當"], cands, dept, attempt=1,
                 departments=directory.departments, dept_residual=q)
    assert dec.action == "hangup"
    assert dec.reason == "no-intent"
    assert dec.speech == ["sounds/zh/goodbye"]


def test_dept_scoped_ask_second_round_falls_back_to_operator(directory):
    """追問後第二輪仍 0 候選 → 既有收斂路徑：fallback → 總機，不再問第三次。"""
    text = "报街业务部"                 # 實撥 #7 的 STT 殘骸（簡體）
    cands, dept, q = directory.search(text)
    first = decide([text], cands, dept, attempt=1,
                   departments=directory.departments, dept_residual=q)
    assert first.reason == "dept-scoped-ask"
    second = decide([text, text], cands, dept, attempt=2,
                    departments=directory.departments, dept_residual=q)
    assert second.action == "fallback"
    assert second.reason == "no-candidate-giveup"
    assert second.speech == ["sounds/zh/not-found"]


def test_pure_filler_query_hangs_up(directory):
    cands, dept, q = directory.search("那個")
    assert cands == [] and dept is None
    dec = decide(["那個"], cands, dept, attempt=1,
                 departments=directory.departments, dept_residual=q)
    assert dec.action == "hangup"
    assert dec.reason == "no-intent"


def test_second_round_vague_answer_goes_to_operator(directory):
    dec = decide(
        ["幫我接客服部", "就那個誰"], [], None, attempt=2,
        departments=directory.departments, dept_residual="誰",
    )
    assert dec.action == "fallback"
    assert dec.reason == "no-candidate-giveup"
    # 先說「無法確認您要找的對象」，再由 dialplan 播 to-operator
    assert dec.speech == ["sounds/zh/not-found"]


def test_second_round_bare_department_still_routes(directory):
    cands, dept, q = directory.search("客服部")
    dec = decide(["幫我接客服部", "客服部"], cands, dept, attempt=2,
                 departments=directory.departments, dept_residual=q)
    assert dec.action == "route_dept"
    assert dec.target == "cs"


@pytest.mark.parametrize("query,dept_id", [
    ("IT部", "it"),
    ("IT單位", "it"),
    ("資訊單位", "it"),
    ("工程處", "rd"),
])
def test_bare_department_suffix_routes(directory, query, dept_id):
    cands, dept, q = directory.search(query)
    assert dept is not None and dept.dept_id == dept_id
    dec = decide([query], cands, dept, attempt=1,
                 departments=directory.departments, dept_residual=q)
    assert dec.action == "route_dept", (query, q, dec.reason)
    assert dec.target == dept_id


def test_no_candidate_retry_keeps_asking(directory):
    dec = decide(["我要找一個不存在的人"], [], None, attempt=1,
                 departments=directory.departments, dept_residual="不存在人")
    assert dec.action == "ask"
    assert dec.reason == "no-candidate-retry"


def test_ambiguous_multi_candidate_asks_once(directory):
    """(d) 區：本地收斂不了的多候選 → 問一句，reason 標明 ambiguous-ask。"""
    cands = [
        Candidate(directory._by_pid["u001"], 0.91),
        Candidate(directory._by_pid["u002"], 0.80),
    ]
    dec = decide(["王偉"], cands, None, attempt=1,
                 departments=directory.departments, dept_residual="王偉")
    assert dec.action == "ask"
    assert dec.reason == "ambiguous-ask:low-score"
    assert dec.speech  # 澄清話術仍在


def test_ambiguous_second_round_falls_back(directory):
    """第 2 輪仍拉鋸 → clarify-exhausted，不再問第三次。"""
    cands = [
        Candidate(directory._by_pid["u001"], 0.91),
        Candidate(directory._by_pid["u002"], 0.80),
    ]
    dec = decide(["王偉", "王偉"], cands, None, attempt=2,
                 departments=directory.departments, dept_residual="王偉")
    assert dec.action == "fallback"
    assert dec.reason == "clarify-exhausted"
    assert dec.speech == ["sounds/zh/not-found"]


def test_attempt2_homophone_still_falls_back():
    d = load_directory(Path(__file__).resolve().parent.parent / "data" / "people.json")
    cands, dept, q = d.search("章家豪")
    dec = decide(["章家豪"], cands, dept, attempt=2,
                 departments=d.departments, dept_residual=q)
    assert dec.action == "fallback"


def test_all_342_decisions_are_ascii():
    from eval import build_cases

    d = load_directory(Path(__file__).resolve().parent.parent / "data" / "people.json")
    for case in build_cases(d.people, d.departments):
        cands, dept, q = d.search(case.query)
        for attempt in (1, 2):
            dec = decide([case.query], cands, dept, attempt=attempt,
                         departments=d.departments, dept_residual=q)
            for value in (dec.action, dec.target, dec.reason, *dec.speech):
                assert value is None or value.isascii(), (case.query, value)


def test_decision_contract_is_ascii(directory):
    decisions = []
    for query in ("業務部", "我要談業務合作", "王偉", "王先生"):
        cands, dept, q = directory.search(query)
        for attempt in (1, 2):
            decisions.append(
                decide([query], cands, dept, attempt=attempt,
                       departments=directory.departments, dept_residual=q)
            )
    for dec in decisions:
        for value in (dec.action, dec.target, dec.reason, *dec.speech):
            assert value is None or value.isascii(), (dec.action, value)
