# SPDX-License-Identifier: AGPL-3.0-or-later
"""dialplan 逐行對照：相對 R1 版只多旁路三行＋AGI 動詞改 result。

`tests/data/dialplan-r1-baseline.txt` 是 R2 動工前的 `[dbn-main]` 片段。這個測試把
「只差這些」變成可執行的契約，不是人工目檢：任何一行被順手改掉、或多插一行，都會紅。
"""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "dialplan.md"
BASELINE = Path(__file__).resolve().parent / "data" / "dialplan-r1-baseline.txt"

BYPASS = (
    " same => n,AGI(agi://127.0.0.1:4573,start,${DBN_WAV}-in.wav,${UNIQUEID},${DBN_SEQ})",
    " same => n,MixMonitor(,r(${DBN_WAV}-in.wav))",
    " same => n,StopMixMonitor()",
)
OLD_AGI = " same => n,AGI(agi://127.0.0.1:4573,${DBN_WAV}.wav,${DBN_ATTEMPT},${DBN_CANDS})"
RESULT_AGI = (" same => n,AGI(agi://127.0.0.1:4573,result,${DBN_WAV}.wav,${UNIQUEID},"
              "${DBN_SEQ},${DBN_ATTEMPT},${DBN_CANDS})")
RECORD = " same => n,Record(${DBN_WAV}.wav,4,10,${DBN_REC_OPTS})"
PLAYBACK = " same => n,Playback(${DBN_SOUND}/processing)"
RECORD_STATUS = (
    ' same => n,GotoIf($["${RECORD_STATUS}"="OPERATOR"]?operator)',
    ' same => n,GotoIf($["${RECORD_STATUS}"="HANGUP"]?done)',
    ' same => n,GotoIf($["${RECORD_STATUS}"="ERROR"]?retry)',
)


def _dialplan_lines() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        if "[dbn-main]" in block:
            return block.splitlines()
    raise AssertionError("docs/dialplan.md 找不到 [dbn-main] code block")


def test_only_bypass_lines_and_result_verb_differ_from_r1():
    new = _dialplan_lines()
    baseline = BASELINE.read_text(encoding="utf-8").splitlines()

    for line in BYPASS:
        assert new.count(line) == 1, f"旁路行必須恰好出現一次：{line}"
    reduced = [line for line in new if line not in BYPASS]

    assert reduced.count(RESULT_AGI) == 1
    assert OLD_AGI not in reduced              # 舊三參數協議必須消失
    assert reduced.index(RESULT_AGI) == baseline.index(OLD_AGI)
    reduced[reduced.index(RESULT_AGI)] = OLD_AGI

    # 還原旁路三行與 AGI 動詞後，必須與 R1 版逐行相同（含註解）
    assert reduced == baseline


def test_record_line_is_character_identical():
    line = RECORD
    assert line in _dialplan_lines()
    assert line in BASELINE.read_text(encoding="utf-8").splitlines()
    assert "4,10,${DBN_REC_OPTS}" in line


def test_bypass_order_and_dispatch_chain_are_intact():
    lines = _dialplan_lines()
    start = lines.index(BYPASS[0])
    mix = lines.index(BYPASS[1])
    record = lines.index(RECORD)
    stop = lines.index(BYPASS[2])

    assert start + 1 == mix < record < stop          # start → MixMonitor → Record → Stop
    assert lines[stop + 1:stop + 4] == list(RECORD_STATUS)   # Stop 無條件、在三個 GotoIf 之前
    assert lines[lines.index(PLAYBACK) + 1] == RESULT_AGI    # processing 位置不動
    assert lines.index(RESULT_AGI) + 1 == lines.index(
        ' same => n,GotoIf($["${AGISTATUS}"!="SUCCESS"]?operator)')


def test_bypass_lines_are_ascii_and_have_no_new_dependencies():
    for line in BYPASS:
        assert line.isascii()
    # C 案：單腿 side file，主檔參數刻意省略；漏 r( 會被當成 options 而不產檔（R0 §3.3）
    assert "MixMonitor(,r(" in BYPASS[1]
