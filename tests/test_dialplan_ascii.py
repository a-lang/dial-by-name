# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/dialplan.md 的 dialplan 片段：去註解後必須全 ASCII。"""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "dialplan.md"


def _dialplan_block() -> str:
    text = DOC.read_text(encoding="utf-8")
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        if "[dbn-main]" in block:
            return block
    raise AssertionError("docs/dialplan.md 找不到 [dbn-main] code block")


def test_dialplan_is_standalone_context():
    block = _dialplan_block()
    assert "[from-internal-custom]" not in block
    assert "[dbn-main]" in block
    assert "[dbn-play-speech]" in block
    assert "${CUT(DBN_SPEECH,&,${DBN_IDX})}" in block
    assert not re.search(r"\bDBN_I\b", block)
    assert not re.search(r"\bDBN_C\b", block)


def test_dialplan_is_ascii_outside_comments():
    bad = []
    for lineno, line in enumerate(_dialplan_block().splitlines(), 1):
        code = line.split(";", 1)[0]
        if not code.isascii():
            bad.append((lineno, line))
    assert not bad, f"dialplan 有非 ASCII（註解除外）：{bad}"


def test_dialplan_uses_ascii_variable_values():
    # 通道變數值只能 ASCII；這條比對常見的中文塞入
    block = _dialplan_block()
    for name in ("DBN_DEPT", "DBN_TARGET", "DBN_SPEECH", "DBN_ACTION"):
        for value in re.findall(rf"{name}=([^\)\s]+)", block):
            assert value.isascii(), (name, value)
