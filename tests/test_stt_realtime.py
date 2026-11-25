# SPDX-License-Identifier: AGPL-3.0-or-later
"""`dialname.stt_realtime` 的離線測試：假 WS 在 127.0.0.1，不碰外網、不需要真 key。"""

from __future__ import annotations

import time

import pytest

from dialname.stt_realtime import (
    DEFAULT_URL,
    RealtimeAuthError,
    RealtimeStt,
    build_url,
    parse_event,
)

from fake_ws import FakeRealtimeServer


def _rt(server, **kwargs) -> RealtimeStt:
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("timeout", 3.0)
    return RealtimeStt(url=server.url, **kwargs)


def _wait(predicate, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待條件逾時")


def test_build_url_carries_r0_params():
    url = build_url()
    assert url.startswith(DEFAULT_URL + "?")
    for part in ("model_id=scribe_v3_live", "audio_format=pcm_8000",
                 "commit_strategy=vad", "vad_silence_threshold_secs=1.0",
                 "language_code=zh"):
        assert part in url
    assert "language_code" not in build_url(language_code=None)


def test_open_sends_api_key_and_query():
    with FakeRealtimeServer() as server:
        rt = _rt(server)
        rt.open()
        try:
            assert "model_id=scribe_v3_live" in server.path
            assert "audio_format=pcm_8000" in server.path
            assert "commit_strategy=vad" in server.path
            assert "vad_silence_threshold_secs=1.0" in server.path
            assert "language_code=zh" in server.path
            assert server.headers.get("xi-api-key") == "test-key"
            assert rt.config["audio_format"] == "pcm_8000"
        finally:
            rt.close()


def test_401_raises_auth_error():
    with FakeRealtimeServer(mode="auth") as server:
        rt = _rt(server)
        with pytest.raises(RealtimeAuthError):
            rt.open()


def test_missing_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    rt = RealtimeStt(url="ws://127.0.0.1:1", timeout=0.1)
    with pytest.raises(RealtimeAuthError):
        rt.open()


def test_pcm_is_chunked_to_one_second():
    with FakeRealtimeServer() as server:
        rt = _rt(server)
        rt.open()
        try:
            rt.send_pcm(bytes(40000))
            _wait(lambda: server.total_bytes == 40000)
            # 16000 B ＝ 8000 Hz × 2 B × 1 s（R0 §5-2：整包 32768 B 會被拒）
            assert server.chunks == [16000, 16000, 8000]
            assert server.sample_rates == {8000}
            assert server.commit_flags == [False, False, False]
        finally:
            rt.close()


def test_commit_flag_goes_on_last_chunk():
    with FakeRealtimeServer() as server:
        rt = _rt(server)
        rt.open()
        try:
            rt.send_pcm(bytes(20000), commit=True)
            _wait(lambda: server.total_bytes == 20000)
            assert server.chunks == [16000, 4000]
            assert server.commit_flags == [False, True]
        finally:
            rt.close()


def test_empty_commit_sends_silence_carrier():
    with FakeRealtimeServer() as server:
        rt = _rt(server)
        rt.open()
        try:
            assert rt.send_pcm(b"", commit=True) == 0
            _wait(lambda: server.total_bytes == 320)
            assert server.chunks == [320]
            assert server.commit_flags == [True]
        finally:
            rt.close()


def test_partial_then_commit_events():
    with FakeRealtimeServer(text="王偉", commit_after=9600) as server:
        rt = _rt(server)
        rt.open()
        try:
            rt.send_pcm(bytes(12800))
            events = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(e.kind == "commit" for e in events):
                events += rt.poll(0.1)
            assert [(e.kind, e.text) for e in events] == [
                ("partial", "王"), ("commit", "王偉"),
            ]
        finally:
            rt.close()


def test_server_close_reports_closed_event():
    with FakeRealtimeServer(mode="drop", drop_after=6400) as server:
        rt = _rt(server)
        rt.open()
        try:
            rt.send_pcm(bytes(8000))
            events = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(e.kind == "closed" for e in events):
                events += rt.poll(0.1)
            assert any(e.kind == "closed" for e in events)
        finally:
            rt.close()


def test_parse_event_ignores_unknown_and_maps_errors():
    assert parse_event('{"message_type":"session_started"}') is None
    assert parse_event("not json") is None
    assert parse_event('{"message_type":"auth_error"}').kind == "error"
    assert parse_event('{"message_type":"partial_transcript","text":"王"}').kind == "partial"
