# SPDX-License-Identifier: AGPL-3.0-or-later
"""ElevenLabs Realtime STT 的薄 WebSocket client（一輪一條連線）。

定案參數見 `.plan/DEVLOG.md` R0 §1.8（刻意寫死在模組常數，不從環境讀）：
`scribe_v3_live`、`pcm_8000` 直送（不上採樣）、`commit_strategy=vad`＋
`vad_silence_threshold_secs=1.0`、`language_code=zh`（與批次 adapter 同值）。

單執行緒擁有連線：`open()` → `send_pcm()`／`poll()` → `close()`。
不做跨執行緒共享（turn manager 的一輪一個 worker 執行緒負責全部呼叫）。
只做「送 PCM、收事件」，不碰正規化與決策（AGENTS.md 契約 1／3）。
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as _ws_connect

DEFAULT_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
MODEL_ID = "scribe_v3_live"
AUDIO_FORMAT = "pcm_8000"
SAMPLE_RATE = 8000
# 每則 ≦1 秒（8 kHz s16 = 16000 B）：R0 §5-2，32768 B 整包會被 chunk_size_exceeded 拒絕。
MAX_CHUNK_BYTES = SAMPLE_RATE * 2
# 沒有新音訊但要收尾時，用 20 ms 靜音當 commit 的載體（協議要求 audio_base_64 必填）。
COMMIT_SILENCE_BYTES = 320

# 這些 message_type 都是錯誤事件（docs event reference）；`auth_error` 另外升級成例外。
ERROR_TYPES = frozenset({
    "error", "auth_error", "quota_exceeded", "input_error", "invalid_request",
    "commit_throttled", "rate_limited", "chunk_size_exceeded", "transcriber_error",
    "unaccepted_terms",
})


class RealtimeError(RuntimeError):
    """WS 連線層失敗（中斷、協議錯誤、連不上）。"""


class RealtimeAuthError(RealtimeError):
    """401/403 或缺 key：key 問題，呼叫端落 fallback（政策已從 PoC 的 hangup 翻案）。"""


class RealtimeUnavailable(RealtimeError):
    """連不上／握手失敗／等不到 session_started。"""


@dataclass(frozen=True)
class RealtimeEvent:
    kind: str          # partial | commit | error | closed
    text: str = ""
    detail: str = ""


def build_url(base: str = DEFAULT_URL, *, language_code: str | None = "zh") -> str:
    query = {
        "model_id": MODEL_ID,
        "audio_format": AUDIO_FORMAT,
        "commit_strategy": "vad",
        "vad_silence_threshold_secs": "1.0",
    }
    if language_code:
        query["language_code"] = language_code
    return f"{base}?{urlencode(query)}"


def parse_event(raw: str | bytes) -> RealtimeEvent | None:
    """把 server 訊息收斂成 RealtimeEvent；不關心的型別回 None。"""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    kind = data.get("message_type", "")
    if kind == "partial_transcript":
        return RealtimeEvent("partial", data.get("text") or "")
    if kind == "committed_transcript":
        return RealtimeEvent("commit", data.get("text") or "")
    if kind in ERROR_TYPES or kind.endswith("_error"):
        return RealtimeEvent("error", detail=kind)
    return None


class RealtimeStt:
    """一輪一條連線的 client。`connect` 可注入（測試用假 WS）。"""

    def __init__(self, api_key: str | None = None, *, url: str | None = None,
                 language_code: str | None = "zh", timeout: float = 10.0,
                 connect=None):
        self.api_key = api_key
        # `url` 是 endpoint（區域變體用），參數一律由 build_url 疊上去，免得漏帶 R0 定案值
        self.url = build_url(url or DEFAULT_URL, language_code=language_code)
        self.timeout = timeout
        self._connect = connect or _ws_connect
        self._ws = None
        self._cm = None
        self.config: dict = {}

    # -- 生命週期 -----------------------------------------------------------
    def open(self) -> None:
        key = self.api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RealtimeAuthError("缺少 ELEVENLABS_API_KEY")
        try:
            cm = self._connect(
                self.url, additional_headers={"xi-api-key": key},
                open_timeout=self.timeout, close_timeout=1.0, max_size=None,
            )
            ws = cm.__enter__()
        except InvalidStatus as exc:
            status = int(getattr(exc.response, "status_code", 0))
            if status in (401, 403):
                raise RealtimeAuthError(f"Realtime STT 認證失敗（{status}）") from exc
            raise RealtimeUnavailable(f"Realtime STT 握手失敗（{status}）") from exc
        except Exception as exc:  # noqa: BLE001 — 連線層錯誤一律視為不可用
            raise RealtimeUnavailable(f"Realtime STT 連線失敗：{exc}") from exc
        self._cm = cm
        self._ws = ws

        # 等 session_started：確認 server 接受這組參數（model／格式／語言）。
        # 音訊來源是磁碟上的旁路檔，等這段不會掉音。
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RealtimeUnavailable("Realtime STT 等不到 session_started")
                try:
                    raw = self._ws.recv(timeout=remaining)
                except TimeoutError as exc:
                    raise RealtimeUnavailable("Realtime STT 等不到 session_started") from exc
                except ConnectionClosed as exc:
                    raise RealtimeUnavailable(f"Realtime STT 握手後斷線：{exc}") from exc
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if data.get("message_type") == "session_started":
                    self.config = data.get("config") or {}
                    return
                event = parse_event(raw)
                if event and event.kind == "error":
                    raise RealtimeError(f"Realtime STT 拒絕連線：{event.detail}")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        cm, self._cm = self._cm, None
        self._ws = None
        if cm is None:
            return
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — 關閉失敗不影響已取得的結果
            pass

    # -- 音訊與事件 ---------------------------------------------------------
    def send_pcm(self, pcm: bytes, *, commit: bool = False) -> int:
        """送一段 PCM，回傳實際當作音訊送出的位元組數（不含收尾用的補靜音）。"""
        if self._ws is None:
            raise RealtimeError("Realtime STT 尚未連線")
        if not pcm:
            if not commit:
                return 0
            pcm = b"\x00" * COMMIT_SILENCE_BYTES
            audio_bytes = 0
        else:
            audio_bytes = len(pcm)

        chunks = [pcm[i:i + MAX_CHUNK_BYTES] for i in range(0, len(pcm), MAX_CHUNK_BYTES)]
        try:
            for index, chunk in enumerate(chunks):
                message = {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(chunk).decode("ascii"),
                    "commit": bool(commit and index == len(chunks) - 1),
                    "sample_rate": SAMPLE_RATE,
                }
                self._ws.send(json.dumps(message))
        except ConnectionClosed as exc:
            raise RealtimeError("Realtime STT 連線中斷") from exc
        return audio_bytes

    def poll(self, timeout: float = 0.0) -> list[RealtimeEvent]:
        """收目前所有可用事件；`timeout` 是「至少等這麼久」而不是總預算。"""
        if self._ws is None:
            raise RealtimeError("Realtime STT 尚未連線")
        events: list[RealtimeEvent] = []
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            wait = 0.0 if timeout <= 0 else max(deadline - time.monotonic(), 0.0)
            try:
                raw = self._ws.recv(timeout=wait)
            except TimeoutError:
                return events
            except ConnectionClosed as exc:
                events.append(RealtimeEvent("closed", detail=str(exc)))
                return events
            event = parse_event(raw)
            if event is not None:
                events.append(event)
