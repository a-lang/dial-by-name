# SPDX-License-Identifier: AGPL-3.0-or-later
"""離線假 ElevenLabs Realtime WS：只實作本專案會用到的子集。

協議照 `.plan/DEVLOG.md` R0 §1.4／§2：`session_started`、`partial_transcript`、
`committed_transcript`；`process_request` 可回 401（驗認證失敗的 fallback 政策）。

模式：
  vad     送 partial，累積到 `commit_after` 就 commit（模擬 VAD）；之後的明確 commit
          只回空字串（真實 API 對已 commit 的靜音尾巴就是這樣）
  silent  從不 partial／commit（沉默來電）
  partial 只送 partial，從不 commit（串流落後）
  eof     只在收到 `commit:true` 時 commit（EOF 收尾）
  drop    累積到 `drop_after` 就斷線；`drop_text` 有值就先送 commit
  auth    握手回 401
  hang    接受連線但不送 `session_started`（open() 卡住，驗 result 的 3 s 預算）
"""

from __future__ import annotations

import base64
import json
import threading

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.sync.server import serve


class FakeRealtimeServer:
    def __init__(self, mode: str = "vad", *, text: str = "王偉", partial: str | None = None,
                 partial_after: int = 6400, commit_after: int = 9600,
                 drop_after: int = 6400, drop_text: str | None = None,
                 api_key: str = "test-key"):
        self.mode = mode
        self.text = text
        self.partial = text[:1] if partial is None else partial
        self.partial_after = partial_after
        self.commit_after = commit_after
        self.drop_after = drop_after
        self.drop_text = drop_text
        self.api_key = api_key

        self.path = ""
        self.headers = Headers()
        self.chunks: list[int] = []
        self.sample_rates: set[int | None] = set()
        self.commit_flags: list[bool] = []
        self.total_bytes = 0
        self.connections = 0

        self._server = serve(self._handle, "127.0.0.1", 0,
                             process_request=self._process_request)
        self.port = self._server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # -- 生命週期 -----------------------------------------------------------
    def __enter__(self) -> "FakeRealtimeServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(2)

    # -- 協議 ---------------------------------------------------------------
    def _process_request(self, connection, request):
        self.path = request.path
        self.headers = request.headers
        if self.mode == "auth":
            return Response(401, "Unauthorized", Headers(), b"")
        if request.headers.get("xi-api-key") != self.api_key:
            return Response(401, "Unauthorized", Headers(), b"")
        return None

    def _send(self, conn, message_type: str, text: str = "") -> None:
        conn.send(json.dumps({"message_type": message_type, "text": text}))

    def _handle(self, conn) -> None:
        self.connections += 1
        if self.mode == "hang":
            self._keep_alive(conn)
            return

        conn.send(json.dumps({
            "message_type": "session_started",
            "session_id": "fake",
            "config": {"sample_rate": 8000, "audio_format": "pcm_8000",
                       "commit_strategy": "vad", "vad_silence_threshold_secs": 1.0},
        }))
        partial_sent = False
        committed = False
        try:
            while True:
                message = json.loads(conn.recv())
                chunk = base64.b64decode(message.get("audio_base_64") or "")
                self.chunks.append(len(chunk))
                self.sample_rates.add(message.get("sample_rate"))
                self.commit_flags.append(bool(message.get("commit")))
                self.total_bytes += len(chunk)

                if message.get("commit"):
                    self._on_commit(conn, committed)
                    committed = True
                    continue

                if not partial_sent and self.total_bytes >= self.partial_after \
                        and self.mode in ("vad", "partial"):
                    self._send(conn, "partial_transcript", self.partial)
                    partial_sent = True
                if not committed and self.total_bytes >= self.commit_after \
                        and self.mode == "vad":
                    self._send(conn, "committed_transcript", self.text)
                    committed = True
                if self.mode == "drop" and self.total_bytes >= self.drop_after:
                    if self.drop_text:
                        self._send(conn, "committed_transcript", self.drop_text)
                    conn.close(1011, "fake drop")
                    return
        except ConnectionClosed:
            return

    def _on_commit(self, conn, already_committed: bool) -> None:
        if already_committed:
            if self.mode == "vad":
                # 已 commit 的段落之後只剩收尾靜音 → 空字串（不可重複送出同一段文字）
                self._send(conn, "committed_transcript", "")
            return
        if self.mode in ("vad", "eof"):
            self._send(conn, "committed_transcript", self.text)
        elif self.mode == "empty":
            # 沉默也會 commit 空字串（R0 §1.6 未證；實測若如此，走既有 stt-empty → retry）
            self._send(conn, "committed_transcript", "")

    @staticmethod
    def _keep_alive(conn) -> None:
        try:
            while conn.recv():
                pass
        except ConnectionClosed:
            return
