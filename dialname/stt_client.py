# SPDX-License-Identifier: AGPL-3.0-or-later
"""ElevenLabs Scribe v2 的薄 adapter：wav → 逐字稿。

只做兩件事：multipart 上傳、把回應收斂成 `Transcript(text, language, words, raw)`。
不碰正規化（那是 `core.normalize` 的工作）、不碰評分。

金鑰只從建構子參數或 `ELEVENLABS_API_KEY` 讀，不落地到任何檔案。
`httpx` 只在真的要打 API 時才 import（建構子注入 http 的路徑不需要它）。

錯誤處理：不吞、不重試。
  401/403 → `SttAuthError`（key 問題，呼叫端停下回報）
  422     → `SttRequestError`（multipart 格式，最可能是 keyterms）
  其他    → `SttError`（附 body 前段供診斷）
  timeout／連線失敗 → httpx 原生例外原樣往上拋
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_MODEL = "scribe_v2"


class SttError(RuntimeError):
    """STT 呼叫失敗。`status_code` 讓呼叫端分辨 401（key 問題）與 5xx（服務問題）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SttAuthError(SttError):
    """401/403：key 無效、沒權限或方案不含此功能——停下回報，不要重試。"""


class SttRequestError(SttError):
    """422：multipart 格式被拒（最可能是 keyterms 欄位）。"""


@dataclass
class Transcript:
    text: str
    language: str | None = None
    words: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _error_for_response(r) -> SttError:
    status = int(getattr(r, "status_code", 0))
    body = (getattr(r, "text", "") or "").strip()[:300]
    detail = f"HTTP {status}" + (f"；body 前段：{body}" if body else "")
    if status in (401, 403):
        return SttAuthError(
            f"STT 認證失敗（{detail}）。請確認 ELEVENLABS_API_KEY 是否正確、方案是否含 Scribe；"
            "不要重試（重試只會被鎖）。",
            status_code=status,
        )
    if status == 422:
        return SttRequestError(
            f"STT 請求格式被拒（{detail}）。最可能是 keyterms 的 multipart 重複欄位格式；"
            "改用 JSON 陣列或逐個 keyterm 對照測試確認。",
            status_code=status,
        )
    return SttError(f"STT 呼叫失敗（{detail}）。", status_code=status)


class SttClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 language: str | None = "zh", keyterms: list[str] | None = None,
                 timeout: float = 120.0, http=None):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        self.model = model
        self.language = language
        self.keyterms = keyterms or []
        self.timeout = timeout
        if http is None:
            import httpx                      # 只有真要打 API 才 import
            http = httpx.Client(timeout=timeout)
        self._http = http

    def transcribe(self, path: str | Path) -> Transcript:
        path = Path(path)
        data = {
            "model_id": self.model,
            "tag_audio_events": "false",
            "timestamps_granularity": "none",
            "num_speakers": "1",
        }
        if self.language:
            data["language_code"] = self.language
        # keyterms 是重複欄位（multipart 陣列），不是 JSON：值給 list，httpx 會展開成
        # 多個同名 part。`data` 必須是 Mapping——傳 list of tuples 會被 httpx 當成
        # raw body（requests 相容路徑），組 body 時炸 TypeError。
        if self.keyterms:
            data["keyterms"] = list(self.keyterms)
        files = {"file": (path.name, path.read_bytes(), "audio/wav")}

        r = self._http.post(API_URL, headers={"xi-api-key": self.api_key or ""},
                            files=files, data=data)
        if r.status_code >= 400:
            raise _error_for_response(r)
        body = r.json()
        return Transcript(
            text=body.get("text", ""),
            language=body.get("language_code"),
            words=body.get("words") or [],
            raw=body,
        )
