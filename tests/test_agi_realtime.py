# SPDX-License-Identifier: AGPL-3.0-or-later
"""兩相協議的離線驗收：handle_start／handle_result＋turn manager＋假 WS。

全部走本機假 WS（`tests/fake_ws.py`），不碰外網、不需要真 key。
失敗對應表見 `.plan/plan-realtime-stt.md` §5；frames／partial 判準見 §11 B6。
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import pytest

from dialname.agi import handle_result, handle_start
from dialname.core import load_directory
from dialname.stt_realtime import RealtimeStt
from dialname.turns import TurnManager

from fake_ws import FakeRealtimeServer

ROOT = Path(__file__).resolve().parents[1]
CALL_ID = "1700000000.1"


def pcm(seconds: float = 1.0) -> bytes:
    return bytes(int(8000 * 2 * seconds))


def write_wav(path: Path, data: bytes, *, append: bool = False) -> None:
    if append:
        with open(path, "ab") as f:
            f.write(data)
        return
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def make_manager(server, *, client_timeout: float = 3.0, **kwargs) -> TurnManager:
    kwargs.setdefault("commit_wait", 0.3)
    kwargs.setdefault("poll_interval", 0.01)
    return TurnManager(
        lambda: RealtimeStt(api_key="test-key", url=server.url, timeout=client_timeout),
        **kwargs,
    )


@pytest.fixture(scope="module")
def directory():
    return load_directory(ROOT / "data" / "people.json")


def _wait(predicate, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待條件逾時")


def _paths(tmp_path: Path, seq: int) -> tuple[Path, Path]:
    return tmp_path / f"rec-{seq}-in.wav", tmp_path / f"rec-{seq}.wav"


def _turn(tmp_path, manager, directory, *, seq=1, prev=None, seconds=1.0,
          side=True, timeout=2.0, attempt=1) -> dict:
    side_path, record_path = _paths(tmp_path, seq)
    if side:
        write_wav(side_path, pcm(seconds))
    write_wav(record_path, pcm(seconds))
    assert handle_start(side_path, CALL_ID, seq, manager=manager) is None   # 不設通道變數
    return handle_result(record_path, CALL_ID, seq, attempt, prev, manager=manager,
                         directory=directory, log_dir=tmp_path / "logs", timeout=timeout)


def _logs(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for f in sorted((tmp_path / "logs").glob("*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()]


def test_happy_path_routes_and_cleans_up(tmp_path, directory):
    with FakeRealtimeServer(text="王小明") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory)

        assert result["action"] == "route"
        assert result["target"] == "2102"
        assert set(result) == {"action", "target", "dept", "speech", "reason", "cands"}
        assert result["cands"].startswith("u003:")

        side_path, record_path = _paths(tmp_path, 1)
        assert not side_path.exists() and not record_path.exists()   # 隱私：wav 即刪

        row = _logs(tmp_path)[-1]
        assert row["seq"] == 1
        assert row["wav"] == str(record_path)
        assert row["stt"]["text"] == "王小明"
        assert row["decision"]["action"] == "route"
        assert row["stream"]["status"] == "ok"
        assert row["stream"]["frames"] == 50          # 16000 B ÷ 320 B（20 ms）
        assert row["stream"]["bypass"] == side_path.name
        assert row["stream"]["commit_ms"] is not None
        assert row["stt"]["ms"] is not None and row["stt"]["ms"] < 300


def test_ws_drop_with_transcript_uses_it(tmp_path, directory):
    with FakeRealtimeServer(mode="drop", drop_after=6400, drop_text="王小明") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory)

        assert result["action"] == "route"
        assert result["target"] == "2102"
        side_path, record_path = _paths(tmp_path, 1)
        assert not side_path.exists() and not record_path.exists()
        assert _logs(tmp_path)[-1]["stream"]["status"] == "dropped"


def test_ws_drop_without_transcript_falls_back(tmp_path, directory):
    with FakeRealtimeServer(mode="drop", drop_after=6400) as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory)

        assert result["action"] == "fallback"
        assert result["reason"] == "stream-error"
        assert _logs(tmp_path)[-1]["stream"]["status"] == "error"
        side_path, record_path = _paths(tmp_path, 1)
        assert not side_path.exists() and not record_path.exists()


def test_auth_error_falls_back_not_hangup(tmp_path, directory):
    """政策翻案：401 不再 hangup，一律落總機（系統故障必須落到人）。"""
    with FakeRealtimeServer(mode="auth") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory)

        assert result["action"] == "fallback"
        assert result["reason"] == "stream-error"
        assert result["speech"] == []
        assert _logs(tmp_path)[-1]["stt_status"] == "auth"


def test_result_budget_is_three_seconds(tmp_path, directory):
    """worker 卡在 open() 時，result 仍只等 3 s 就放手（3 s 是 result 端總預算）。"""
    with FakeRealtimeServer(mode="hang") as server:
        manager = make_manager(server, client_timeout=10.0)
        entered = time.monotonic()
        result = _turn(tmp_path, manager, directory, timeout=3.0)
        elapsed = time.monotonic() - entered

        assert result["action"] == "fallback"
        assert result["reason"] == "stream-error"
        assert _logs(tmp_path)[-1]["stt_status"] == "timeout"
        assert 2.9 <= elapsed < 5.0


def test_silence_retries_and_carries_candidates(tmp_path, directory):
    """無 commit 且全程無 partial → retry（不可灌總機），並帶回上一輪候選。"""
    with FakeRealtimeServer(mode="silent") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory, prev="u003:0.900&u004:0.800")

        assert result["action"] == "retry"
        assert result["reason"] == "no-speech"
        assert result["cands"] == "u003:0.900&u004:0.800"
        row = _logs(tmp_path)[-1]
        assert row["stt_status"] == "no-speech"
        assert row["stream"]["frames"] == 50          # 有音訊、只是沒講話
        assert row["stream"]["partials"] == 0


def test_partial_without_commit_falls_back(tmp_path, directory):
    """無 commit 但曾有 partial → 串流落後視同故障 → fallback（B6）。"""
    with FakeRealtimeServer(mode="partial") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory)

        assert result["action"] == "fallback"
        assert result["reason"] == "stream-stalled"
        row = _logs(tmp_path)[-1]
        assert row["stt_status"] == "partial-only"
        assert row["stream"]["partials"] > 0


def test_empty_commit_retries_like_batch_transport(tmp_path, directory):
    """空逐字稿走既有 `stt-empty → retry` 路徑（R0 §1.6 的未證事項若成立也不改結局）。"""
    with FakeRealtimeServer(mode="empty") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory)

        assert result["action"] == "retry"
        assert result["reason"] == "stt-empty"
        row = _logs(tmp_path)[-1]
        assert row["stt_status"] == "empty"
        assert row["stream"]["frames"] > 0


def test_result_without_session_falls_back(tmp_path, directory):
    """start 崩潰／服務重啟後 session 不見：result 仍要刪掉手上的 Record 檔。"""
    manager = TurnManager(lambda: RealtimeStt(api_key="test-key", url="ws://127.0.0.1:1"))
    record = tmp_path / "rec-9.wav"
    write_wav(record, pcm(0.2))
    result = handle_result(record, CALL_ID, 9, 1, "", manager=manager,
                           directory=directory, log_dir=tmp_path / "logs")

    assert result["action"] == "fallback"
    assert result["reason"] == "result-no-session"
    assert _logs(tmp_path)[-1]["stream"]["status"] == "no-session"
    assert not record.exists()


def test_gate_full_stub_falls_back_and_cleans_up(tmp_path, directory):
    with FakeRealtimeServer(mode="silent") as server:
        manager = make_manager(server, max_sessions=1)
        side1, record1 = _paths(tmp_path, 1)
        write_wav(side1, pcm())
        write_wav(record1, pcm())
        session1 = manager.start(CALL_ID, 1, side1)          # 佔住唯一的閘

        side2, record2 = _paths(tmp_path, 2)
        write_wav(side2, pcm())
        write_wav(record2, pcm())
        stub = manager.start(CALL_ID, 2, side2)
        assert stub.stub is True and session1.stub is False

        result = handle_result(record2, CALL_ID, 2, 1, "", manager=manager,
                               directory=directory, log_dir=tmp_path / "logs")
        assert result["action"] == "fallback"
        assert result["reason"] == "session-stub"
        assert not side2.exists() and not record2.exists()
        # 第一輪仍活著（stub 不能清別人的檔）
        assert side1.exists() and record1.exists()

        assert manager.claim(CALL_ID, 1) is session1
        manager.finish(session1)
        assert not side1.exists() and not record1.exists()


def test_sweeper_expires_and_unlinks(tmp_path, directory):
    """result 沒來的分支（OPERATOR／HANGUP／ERROR）：TTL 到就 close WS＋unlink。"""
    with FakeRealtimeServer(mode="silent") as server:
        manager = make_manager(server, ttl=0.4)
        side, record = _paths(tmp_path, 1)
        write_wav(side, pcm())
        write_wav(record, pcm())
        manager.start(CALL_ID, 1, side)

        _wait(lambda: server.total_bytes > 0)     # 確認 WS 真的在串流
        time.sleep(0.5)
        assert manager.sweep() == [f"{CALL_ID}:1"]
        assert manager.active() == 0
        assert not side.exists() and not record.exists()


def test_result_is_the_end_signal_not_file_stall(tmp_path, directory):
    """錄音中本來就會停 ~2.048 s：檔案停滯不是 EOF，result 才是收工訊號。"""
    with FakeRealtimeServer(mode="eof", text="王小明") as server:
        manager = make_manager(server)
        side, record = _paths(tmp_path, 1)
        write_wav(side, pcm(0.4))
        write_wav(record, pcm(0.4))
        session = manager.start(CALL_ID, 1, side)

        _wait(lambda: session.frames == 20)
        time.sleep(0.7)                           # 遠超過 500 ms 的停滯
        assert session.state == "streaming"
        assert session.final.is_set() is False

        write_wav(side, pcm(0.4), append=True)    # 停滯後又長大
        _wait(lambda: session.frames == 40)

        result = handle_result(record, CALL_ID, 1, 1, "", manager=manager,
                               directory=directory, log_dir=tmp_path / "logs")
        assert result["action"] == "route"
        assert result["target"] == "2102"
        assert _logs(tmp_path)[-1]["stream"]["frames"] == 40


def test_frames_zero_falls_back(tmp_path, directory):
    with FakeRealtimeServer(mode="silent") as server:
        manager = make_manager(server)
        result = _turn(tmp_path, manager, directory, side=False)

        assert result["action"] == "fallback"
        assert result["reason"] == "bypass-empty"
        assert _logs(tmp_path)[-1]["stt_status"] == "frames-0"
        assert not _paths(tmp_path, 1)[1].exists()


def test_xiaomei_second_turn_narrows_to_person(tmp_path, directory):
    """`DBN_CANDS` 缺腿時，第二輪「客服部」會變 route_dept（轉 2200）——必紅的防呆。"""
    with FakeRealtimeServer(text="小美") as server1:
        manager = make_manager(server1)
        first = _turn(tmp_path, manager, directory, seq=1)
        assert first["action"] == "ask"
        assert first["cands"] == "u008:0.950&u009:0.950"

    with FakeRealtimeServer(text="客服部") as server2:
        manager._connect = lambda: RealtimeStt(api_key="test-key", url=server2.url,
                                               timeout=3.0)
        second = _turn(tmp_path, manager, directory, seq=2, prev=first["cands"],
                       attempt=2)

    assert second["action"] == "route"
    assert second["target"] == "2201"          # 陳美玲（客服部）
    assert second["dept"] == ""
    assert second["cands"] == "u008:0.950"
