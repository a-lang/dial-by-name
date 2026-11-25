# SPDX-License-Identifier: AGPL-3.0-or-later
"""AGI 薄協議層：初始 env、SET VARIABLE、speech 以 & 串接。"""

from __future__ import annotations

import io

from dialname.agi import AgiSession, run_session


def test_result_verb_sets_six_vars():
    env = (
        b"agi_request: agi://127.0.0.1:4573\n"
        b"agi_uniqueid: 123.45\n"
        b"agi_arg_1: result\n"
        b"agi_arg_2: /tmp/a.wav\n"
        b"agi_arg_3: 123.45\n"
        b"agi_arg_4: 2\n"
        b"agi_arg_5: 1\n"
        b"agi_arg_6: u001:0.900&u002:0.800\n\n"
    )
    rfile = io.BytesIO(env + b"200 result=1\n" * 6)
    wfile = io.BytesIO()
    seen = {}

    def handler(env):
        seen.update(env)
        return {
            "action": "route_dept",
            "target": "sales",
            "dept": "sales",
            "speech": ["transferring", "dept-sales"],
            "reason": "dept-only",
            "cands": "",
        }

    run_session(AgiSession(rfile, wfile), handlers={"result": handler})

    assert seen["agi_arg_2"] == "/tmp/a.wav"
    assert seen["agi_arg_6"] == "u001:0.900&u002:0.800"
    out = wfile.getvalue().decode()
    for line in (
        'SET VARIABLE DBN_ACTION "route_dept"',
        'SET VARIABLE DBN_TARGET "sales"',
        'SET VARIABLE DBN_DEPT "sales"',
        'SET VARIABLE DBN_SPEECH "transferring&dept-sales"',
        'SET VARIABLE DBN_REASON "dept-only"',
        'SET VARIABLE DBN_CANDS ""',
    ):
        assert line in out


def test_start_verb_sets_no_vars():
    """start 立即返回；成功或失敗都不寫通道變數（dialplan 已預設 fallback）。"""
    env = (
        b"agi_uniqueid: 123.45\n"
        b"agi_arg_1: start\n"
        b"agi_arg_2: /tmp/a-1-in.wav\n"
        b"agi_arg_3: 123.45\n"
        b"agi_arg_4: 1\n\n"
    )
    wfile = io.BytesIO()
    seen = {}

    def handler(env):
        seen.update(env)
        return None

    run_session(AgiSession(io.BytesIO(env), wfile), handlers={"start": handler})
    assert seen["agi_arg_1"] == "start"
    assert wfile.getvalue() == b""


def test_unknown_verb_is_rejected():
    rfile = io.BytesIO(b"agi_arg_1: legacy\n\n")
    wfile = io.BytesIO()
    try:
        run_session(AgiSession(rfile, wfile), handlers={"start": lambda env: None,
                                                        "result": lambda env: {}})
    except Exception as exc:  # AgiProtocolError
        assert "legacy" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown verb should raise")


def test_empty_value_is_quoted():
    session = AgiSession(io.BytesIO(b"200 result=1\n"), io.BytesIO())
    session.set_variable("DBN_TARGET", "")
    assert session.wfile.getvalue() == b'SET VARIABLE DBN_TARGET ""\n'


def test_520_multiline_is_consumed():
    rfile = io.BytesIO(
        b"520-Invalid command syntax.  Proper usage follows:\n"
        b"Usage: SET VARIABLE <variable> <value>\n"
        b"520 End of proper usage.\n"
        b"200 result=1\n"
    )
    session = AgiSession(rfile, io.BytesIO())
    try:
        session.set_variable("DBN_TARGET", "2101")
    except Exception as exc:
        assert "語法錯誤" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("520 should raise")
    # 重要的不只是拋錯，而是 usage 三行已讀完，下一個指令不會讀到殘留
    assert session._readline() == "200 result=1"


def test_520_single_line_is_consumed():
    rfile = io.BytesIO(
        b"520 Invalid command syntax. Proper usage not available.\n"
        b"200 result=1\n"
    )
    session = AgiSession(rfile, io.BytesIO())
    try:
        session.set_variable("DBN_TARGET", "2101")
    except Exception as exc:
        assert "語法錯誤" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("single-line 520 should raise")
    assert session._readline() == "200 result=1"


def test_unsafe_value_is_rejected():
    session = AgiSession(io.BytesIO(b"200 result=1\n"), io.BytesIO())
    for bad in ("a\nb", 'a"b', "a\\b"):
        try:
            session.set_variable("DBN_REASON", bad)
        except Exception as exc:
            assert "不安全" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"unsafe value accepted: {bad!r}")
