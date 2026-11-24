# SPDX-License-Identifier: AGPL-3.0-or-later
"""只講名／小名的 alias 政策（2026-11-16 定案）。

規則：
  - 查詢首字**不是**名冊裡的姓 → 只比對 alias（同音）
  - alias 命中 1 人 → 直接轉；命中多人 → 追問部門；查無但像某人的名 → 轉總機
  - 兩者皆非（亂句）→ 照原本流程（掛線），不把垃圾句送進總機

這裡的名冊刻意設計成：
  - 小美／筱美 是同音 alias（兩人）→ 追問部門
  - 小林 是同音 alias（一人）→ 直接轉
  - 美玲／志明 沒有任何 alias → 查無
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dialname.core import load_directory
from dialname.decision import decide
from dialname.agi import handle_turn

ROSTER = {
    "source_directory": "PBX Internal",
    "departments": [
        {"dept_id": "sales", "name": "業務部", "aliases": ["業務"]},
        {"dept_id": "cs", "name": "客服部", "aliases": ["客服"]},
        {"dept_id": "adm", "name": "行政部", "aliases": ["行政"]},
    ],
    "people": [
        {
            "pid": "u001", "name": "王偉", "directory": "PBX Internal",
            "dept": "業務部", "extension": "2101",
            "last_name": "王", "first_name": "偉", "aliases": ["David Wang"],
        },
        {
            "pid": "u002", "name": "陳美玲", "directory": "PBX Internal",
            "dept": "客服部", "extension": "2201",
            "last_name": "陳", "first_name": "美玲", "aliases": ["小美"],
        },
        {
            "pid": "u003", "name": "程美玲", "directory": "PBX Internal",
            "dept": "行政部", "extension": "6101",
            "last_name": "程", "first_name": "美玲", "aliases": ["筱美"],
        },
        {
            "pid": "u004", "name": "林志明", "directory": "PBX Internal",
            "dept": "客服部", "extension": "2202",
            "last_name": "林", "first_name": "志明", "aliases": ["小林"],
        },
    ],
}


class FakeStt:
    def __init__(self, text):
        self.text = text

    def transcribe(self, path):
        return SimpleNamespace(text=self.text, language="zho")


@pytest.fixture()
def directory(tmp_path):
    path = tmp_path / "people.json"
    path.write_text(json.dumps(ROSTER, ensure_ascii=False), encoding="utf-8")
    return load_directory(path)


def _decide(directory, text, attempt=1):
    cands, dept, q, meta = directory.search_meta(text)
    form = "alias" if meta["alias"] else ("given" if meta["given_like"] else None)
    return decide(
        [text], cands, dept, attempt=attempt,
        departments=directory.departments, dept_residual=q,
        surnames=directory.surnames, name_form=form,
    )


# ── alias 命中：1 人 → 直接轉 ────────────────────────────────────────────
def test_alias_literal_unique_routes(directory):
    dec = _decide(directory, "David Wang")
    assert dec.action == "route"
    assert dec.target == "2101"


def test_alias_homophone_unique_routes(directory):
    """「小琳」沒人登記，但與「小林」同音 → 視為命中，直接轉。"""
    dec = _decide(directory, "小琳")
    assert dec.action == "route"
    assert dec.target == "2202"


def test_alias_unique_with_department_via_handle_turn(directory, tmp_path):
    result = handle_turn(
        "call.wav", 1, stt=FakeStt("我想找小林"), directory=directory,
        log_dir=tmp_path / "logs", call_id="1.1",
    )
    assert result["action"] == "route"
    assert result["target"] == "2202"


# ── alias 命中：多人（同音）→ 追問部門 ───────────────────────────────────
def test_alias_homophone_group_asks(directory):
    dec = _decide(directory, "小美")          # 小美(u002) / 筱美(u003) 同音
    assert dec.action == "ask"
    assert dec.reason == "alias-ask"


def test_alias_homophone_group_asks_with_surrounding_words(directory):
    """2026-11-17 驗收回歸：「好，請問小美在嗎？」曾因殘留「好請問」而漏掉筱美，
    只剩字面命中的陳美玲以 0.95 直轉。整串鍵比對改成 2–4 字窗後要恢復成問部門。
    """
    dec = _decide(directory, "好，請問小美在嗎？")
    assert dec.action == "ask"
    assert dec.reason == "alias-ask"


def test_alias_group_second_turn_is_not_asked_again(directory):
    dec = _decide(directory, "小美", attempt=2)
    assert dec.action == "fallback"
    assert dec.reason == "clarify-exhausted"


def test_alias_homophone_group_speech_is_department_question(directory):
    """小美／筱美 案例零變動：話術仍是問部門。"""
    dec = _decide(directory, "小美")
    assert dec.speech == ["sounds/zh/q-which-dept"]


def _alias_pair_roster(second_dept: str) -> dict:
    """兩人共用「小安」、全名不同音、不同姓（2026-11-20 實撥 BUG 的最小名冊）。"""
    return {
        "source_directory": "PBX Internal",
        "departments": [
            {"dept_id": "hr", "name": "人資部", "aliases": ["人資"]},
            {"dept_id": "it", "name": "資訊部", "aliases": ["資訊"]},
        ],
        "people": [
            {
                "pid": "u101", "name": "何俊賢", "directory": "PBX Internal",
                "dept": "人資部", "extension": "3101",
                "last_name": "何", "first_name": "俊賢", "aliases": ["小安"],
            },
            {
                "pid": "u102", "name": "宋芷賢", "directory": "PBX Internal",
                "dept": second_dept, "extension": "3201",
                "last_name": "宋", "first_name": "芷賢", "aliases": ["小安"],
            },
        ],
    }


def _load_roster(tmp_path, roster):
    path = tmp_path / "people.json"
    path.write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    return load_directory(path)


def test_alias_group_different_full_names_asks_department(tmp_path):
    """全名不像（何俊賢／宋芷賢）仍要問部門，不可退回「請說全名」。"""
    directory = _load_roster(tmp_path, _alias_pair_roster("資訊部"))
    dec = _decide(directory, "小安")
    assert dec.action == "ask"
    assert dec.reason == "alias-ask"
    assert dec.speech == ["sounds/zh/q-which-dept"]


def test_alias_group_same_department_keeps_full_name_question(tmp_path):
    """同部門的 alias 群：問部門沒有鑑別力 → 維持請說全名＋按 0 轉該部門。"""
    directory = _load_roster(tmp_path, _alias_pair_roster("人資部"))
    dec = _decide(directory, "小安")
    assert dec.action == "ask"
    assert dec.reason == "alias-ask"
    assert dec.speech == ["sounds/zh/q-say-full-name"]
    assert ("0", "hr", "人資部") in dec.options


def test_alias_group_narrowed_by_department(directory):
    """「客服部的小美」→ 部門收斂到 u002，直接轉。"""
    dec = _decide(directory, "客服部的小美")
    assert dec.action == "route"
    assert dec.target == "2201"


# ── 只講名、查無 alias → 總機（不是掛線）──────────────────────────────
def test_given_name_without_alias_goes_operator(directory):
    dec = _decide(directory, "美玲")
    assert dec.action == "fallback"
    assert dec.reason == "alias-not-found"


def test_given_name_with_surrounding_words_goes_operator(directory):
    """同一句話加了「好，請問…在嗎」也要轉總機，不可以變成掛線（無意圖）。"""
    dec = _decide(directory, "好，請問美玲在嗎？")
    assert dec.action == "fallback"
    assert dec.reason == "alias-not-found"


def test_misheard_given_name_goes_operator(directory):
    """實撥回歸測試：`美玲` 被 STT 聽成 `梅林`，不得再轉給「林志明」。"""
    dec = _decide(directory, "梅林")
    assert dec.action == "fallback"
    assert dec.reason == "alias-not-found"
    assert dec.target is None


def test_given_name_via_handle_turn_is_fallback(directory, tmp_path):
    result = handle_turn(
        "call.wav", 1, stt=FakeStt("我要找美玲"), directory=directory,
        log_dir=tmp_path / "logs", call_id="1.2",
    )
    assert result["action"] == "fallback"
    assert result["reason"] == "alias-not-found"


# ── 亂句不能被送進總機 ────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["我要訂便當", "我的網路壞了", "那個"])
def test_garbage_still_hangs_up(directory, text):
    dec = _decide(directory, text)
    assert dec.action == "hangup"
    assert dec.reason == "no-intent"


# ── 姓氏路徑不受影響 ──────────────────────────────────────────────────
def test_surname_path_unchanged(directory):
    dec = _decide(directory, "我要找林志明")
    assert dec.action == "route"
    assert dec.target == "2202"


def test_full_name_of_alias_owner_still_routes_by_name(directory):
    dec = _decide(directory, "陳美玲")
    assert dec.action in ("route", "ask")
    assert dec.target in (None, "2201")
