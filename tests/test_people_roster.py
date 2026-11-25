# SPDX-License-Identifier: AGPL-3.0-or-later
"""`data/people.json` 的守門測試：alias 政策的文件承諾。

alias 內嵌在名冊裡是 **PoC 的暫時形狀**（正式階段 userman 沒有 alias 欄位，
要搬到 sidecar `aliases.json`，見 `docs/design.md` §名冊來源）。在那之前，
`docs/dialplan.md` §4.2 的三條預期（阿明直轉／小美問部門／美玲轉總機）
就靠這份測試守住；名冊被改動時這裡會先紅。

`SINGLE_CANDIDATE_FLOOR` 的兩個方向也順便釘住：下限之下轉總機、
下限之上（楊芬 0.80）照樣直轉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dialname.core import load_directory
from dialname.decision import decide

ROOT = Path(__file__).resolve().parent.parent
ROSTER = ROOT / "data" / "people.json"

# §4.2 驗收用的三個 alias（`data/people.json` 內嵌）
DEMO_ALIASES = {"u006": "阿明", "u008": "小美", "u009": "筱美"}


@pytest.fixture()
def roster():
    return load_directory(ROSTER)


def test_roster_keeps_demo_aliases():
    """名冊改了、alias 被移除 → §4.2 的驗收會靜默失去前提，這裡先擋。"""
    d = load_directory(ROSTER)
    missing = [
        f"{pid}:{alias}"
        for pid, alias in DEMO_ALIASES.items()
        if d.person(pid) is None or alias not in d.person(pid).aliases
    ]
    assert missing == []


def _decide(directory, text, attempt=1):
    cands, dept, q, meta = directory.search_meta(text)
    form = "alias" if meta["alias"] else ("given" if meta["given_like"] else None)
    return decide(
        [text], cands, dept, attempt=attempt,
        departments=directory.departments, dept_residual=q,
        surnames=directory.surnames, name_form=form,
    )


def test_unique_alias_routes(roster):
    dec = _decide(roster, "阿明")
    assert dec.action == "route"
    assert dec.target == "3102"


def test_homophone_aliases_ask_department(roster):
    dec = _decide(roster, "小美")
    assert dec.action == "ask"
    assert dec.reason == "alias-ask"


def test_homophone_aliases_ask_department_with_surrounding_words(roster):
    """§4.2 實撥劇本原句：加了「好，請問…在嗎」仍要問部門（2026-11-17 回歸）。"""
    dec = _decide(roster, "好，請問小美在嗎？")
    assert dec.action == "ask"
    assert dec.reason == "alias-ask"


def test_ascii_period_run_after_interjection(roster):
    """STT 常吐「呃......」再接姓名：句點不剝掉會讓後續前綴／稱謂全失效。

    2026-11-25 實撥：Realtime 逐字稿「呃......徐先生。」「啊......鄭博文。」——
    ASCII `.` 不在 `PUNCT_RE` 裡，`normalize()` 之後殘留「呃......」，於是
    `_strip_fillers` 剝掉「呃」就停在 `.`，姓名檢索 0 候選（部門偵測是子字串掃描，
    不受影響，所以症狀是「部門聽得到、人找不到」）。
    """
    dec = _decide(roster, "呃......王先生。")
    assert dec.action == "fallback"
    assert dec.reason == "surname-only"

    dec = _decide(roster, "啊......鄭博文。")
    assert dec.action == "route"
    assert dec.target == "3104"
    assert dec.reason == "direct:confident"

    dec = _decide(roster, "呃......阿明。")
    assert dec.action == "route"
    assert dec.target == "3102"


def test_low_confidence_single_candidate_goes_operator(roster):
    """「小米」0.51 命中王小明：只有一個候選也不該轉給陌生人（2026-11-17 驗收）。

    這是 `SINGLE_CANDIDATE_FLOOR` 的下限案例；下限之上的唯一候選仍要轉得出去
    （見 `test_unique_candidate_above_floor_routes`）。
    """
    dec = _decide(roster, "小米")
    assert dec.action == "fallback"
    assert dec.reason == "low-confidence-single"
    assert dec.target is None


def test_unique_candidate_above_floor_routes(roster):
    """漏字查詢（杨芬→楊淑芬 0.80）仍在唯一候選的路徑上直轉，不被下限誤殺。"""
    dec = _decide(roster, "杨芬")
    assert dec.action == "route"
    assert dec.target == "6102"
    assert dec.reason == "single-candidate"


def test_unregistered_given_name_goes_operator(roster):
    dec = _decide(roster, "美玲")
    assert dec.action == "fallback"
    assert dec.reason == "alias-not-found"
    assert dec.target is None
