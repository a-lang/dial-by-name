# SPDX-License-Identifier: AGPL-3.0-or-later
"""agi_server 的兩相路由＋TTL sweeper＋sweeper liveness。

路由在 `dialname.agi.run_session`：`agi_arg_1` 是動詞。這裡用 `agi_server.make_current`
接線後跑真正的協議層，釘住 start 不設變數、result 全送六個變數。
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

import agi_server
from dialname.agi import AgiProtocolError, AgiSession, run_session
from dialname.core import load_directory
from dialname.health import HEARTBEAT_MAX_AGE, latest_jsonl, read_health, write_heartbeat
from dialname.stt_realtime import RealtimeUnavailable
from dialname.turns import TurnManager

ROOT = Path(__file__).resolve().parents[1]
CALL_ID = "1700000000.1"


@pytest.fixture(scope="module")
def directory():
    return load_directory(ROOT / "data" / "people.json")


class RecordingManager:
    """只記錄呼叫；result 拿不到 session 也能驗「六變數全送」。"""

    def __init__(self):
        self.started: list[tuple[str, str, str]] = []
        self.finished = None
        self._session = None

    def start(self, call_id, seq, side_wav=None):
        self.started.append((str(call_id), str(seq), str(side_wav)))

    def claim(self, call_id, seq):
        session, self._session = self._session, None
        return session

    def finish(self, session):
        self.finished = session


def _drive(handlers, env: dict[str, str]) -> str:
    payload = "".join(f"{key}: {value}\n" for key, value in env.items()) + "\n"
    rfile = io.BytesIO(payload.encode() + b"200 result=1\n" * 6)
    wfile = io.BytesIO()
    run_session(AgiSession(rfile, wfile), handlers=handlers)
    return wfile.getvalue().decode()


def _wait(predicate, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待條件逾時")


def test_start_verb_registers_and_sets_no_variables(tmp_path, directory):
    manager = RecordingManager()
    current = agi_server.make_current(directory, manager=manager, log_dir=tmp_path / "logs")
    side = str(tmp_path / "rec-1-in.wav")

    out = _drive(current.handlers, {
        "agi_arg_1": "start", "agi_arg_2": side,
        "agi_arg_3": CALL_ID, "agi_arg_4": "1",
    })

    assert out == ""                       # start 不設任何通道變數
    assert manager.started == [(CALL_ID, "1", side)]


def test_start_failure_also_sets_no_variables(tmp_path, directory):
    """閘滿＝stub；start 仍然回 None（分派鏈在 result 之後，寫了也會被覆寫）。"""
    manager = TurnManager(lambda: object(), max_sessions=0)
    current = agi_server.make_current(directory, manager=manager, log_dir=tmp_path / "logs")

    out = _drive(current.handlers, {
        "agi_arg_1": "start", "agi_arg_2": str(tmp_path / "rec-1-in.wav"),
        "agi_arg_3": CALL_ID, "agi_arg_4": "1",
    })

    assert out == ""
    assert manager.active() == 1           # stub 有登記，result／sweeper 才清得到檔


def test_result_verb_sends_all_six_variables(tmp_path, directory):
    manager = RecordingManager()
    current = agi_server.make_current(directory, manager=manager, log_dir=tmp_path / "logs")
    record = tmp_path / "rec-1.wav"
    record.write_bytes(b"")

    out = _drive(current.handlers, {
        "agi_arg_1": "result", "agi_arg_2": str(record), "agi_arg_3": CALL_ID,
        "agi_arg_4": "1", "agi_arg_5": "2", "agi_arg_6": "u003:0.900",
    })

    lines = [line for line in out.splitlines() if line.startswith("SET VARIABLE")]
    assert len(lines) == 6
    assert 'SET VARIABLE DBN_ACTION "fallback"' in lines
    assert 'SET VARIABLE DBN_REASON "result-no-session"' in lines
    assert 'SET VARIABLE DBN_CANDS "u003:0.900"' in lines   # 下一輪還要收斂
    assert not record.exists()                               # result 即刪 Record 檔


def test_missing_result_args_raise(tmp_path, directory):
    current = agi_server.make_current(directory, manager=RecordingManager(),
                                      log_dir=tmp_path / "logs")
    with pytest.raises(AgiProtocolError):
        _drive(current.handlers, {
            "agi_arg_1": "result", "agi_arg_3": CALL_ID, "agi_arg_4": "1",
        })


def test_result_values_are_still_checked_at_protocol_boundary(tmp_path, directory):
    current = agi_server.make_current(directory, manager=RecordingManager(),
                                      log_dir=tmp_path / "logs")
    handlers = {"result": lambda env: {
        "action": "route", "target": "2101", "dept": "", "speech": [],
        "reason": 'bad"quote', "cands": "",
    }}
    with pytest.raises(AgiProtocolError):
        run_session(AgiSession(io.BytesIO(b"agi_arg_1: result\n\n"), io.BytesIO()),
                    handlers=handlers)


# --------------------------------------------------------------------------- #
# sweeper：TTL 收尾＋heartbeat；死掉由 status 的 heartbeat 判定
# --------------------------------------------------------------------------- #
def _unavailable():
    raise RealtimeUnavailable("no ws")


def test_sweeper_clears_expired_session_and_reports_active(tmp_path):
    manager = TurnManager(_unavailable, ttl=0.05, commit_wait=0.05, poll_interval=0.01)
    side, record = tmp_path / "rec-1-in.wav", tmp_path / "rec-1.wav"
    side.write_bytes(b"x")
    record.write_bytes(b"x")
    manager.start(CALL_ID, 1, side)

    health = tmp_path / "agi.health.json"
    sweeper = agi_server.Sweeper(manager, health, interval=0.02, pid=4242)
    sweeper.start()
    try:
        _wait(lambda: manager.active() == 0)
        _wait(lambda: not side.exists() and not record.exists())
        _wait(lambda: read_health(health, pid=4242).active == 0)
        status = read_health(health, pid=4242)
        assert status.alive and status.reason == "ok"
        assert status.age < 1.0
    finally:
        sweeper.stop()
        sweeper.join(2)


def test_dead_sweeper_is_detected_by_stale_heartbeat(tmp_path, monkeypatch):
    """sweeper 執行緒死掉 → heartbeat 停更 → status 判 DEAD（行程與 port 還在也沒用）。"""
    monkeypatch.setattr(threading, "excepthook", lambda args: None)

    class Broken:
        def sweep(self):
            raise RuntimeError("boom")

        def active(self):
            return 1

    health = tmp_path / "agi.health.json"
    sweeper = agi_server.Sweeper(Broken(), health, interval=0.01, pid=7)
    sweeper.start()
    sweeper.join(2)

    assert not sweeper.is_alive()
    status = read_health(health, pid=7, now=time.time() + HEARTBEAT_MAX_AGE + 1)
    assert not status.alive and status.reason == "stale"


def test_health_rejects_other_process_heartbeat(tmp_path):
    health = tmp_path / "agi.health.json"
    write_heartbeat(health, pid=111, active=2)
    status = read_health(health, pid=222)
    assert not status.alive and status.reason == "pid-mismatch"


def test_latest_jsonl_reports_last_round_timestamp(tmp_path):
    logs = tmp_path / "calls"
    logs.mkdir()
    path = logs / "20261124.jsonl"
    path.write_text('{"ts":"2026-11-24T20:00:00+08:00"}\n'
                    '{"ts":"2026-11-24T20:01:00+08:00"}\n', encoding="utf-8")
    assert latest_jsonl(logs) == (path, "2026-11-24T20:01:00+08:00")
    assert latest_jsonl(tmp_path / "missing") is None
