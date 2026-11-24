# SPDX-License-Identifier: AGPL-3.0-or-later
"""`dialname.stt_client` 的離線測試：不打網路、不需要 API key。

兩層分工：
- multipart 上傳用 `httpx.MockTransport`——走**真的** httpx 編碼器，驗 wire format。
  假 http 只驗得到「我們傳了什麼」，驗不出 httpx 收不收（`data` 傳 list of tuples
  就是這樣溜過去的）。
- 錯誤碼 → 例外型別用假 HTTP 物件，不需要編碼。
"""

from __future__ import annotations

import re
import subprocess
import sys

import httpx
import pytest

from dialname.stt_client import SttAuthError, SttClient, SttError, SttRequestError, Transcript


class FakeResp:
    def __init__(self, body, status_code=200, text=""):
        self._body = body
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, resp=None):
        self.calls = []
        self._resp = resp or FakeResp({"text": "我要找王偉麻煩一下", "language_code": "zho",
                                       "words": []})

    def post(self, url, headers=None, files=None, data=None):
        self.calls.append(dict(url=url, headers=headers, files=files, data=data))
        return self._resp


def _multipart_fields(content_type: str, body: str) -> list[tuple[str, str]]:
    """把 multipart body 拆成 (name, value)。只給測試用；拆不動就讓斷言紅。"""
    boundary = content_type.split("boundary=", 1)[1]
    fields = []
    for chunk in body.split("--" + boundary)[1:-1]:
        head, _, value = chunk.partition("\r\n\r\n")
        match = re.search(r'name="([^"]+)"', head)
        if match:
            fields.append((match.group(1), value.rstrip("\r\n")))
    return fields


def test_transcribe_sends_expected_multipart(tmp_path):
    """用 MockTransport 走真 httpx 編碼：驗 wire format，不驗我們傳了什麼。

    `data` 必須是 Mapping；傳 list of tuples 會被 httpx 當成 raw body 而 TypeError
    （2026-11-13 實機踩到，假 http 完全看不到）。
    """
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF0000WAVE")
    seen = {}

    def handler(request):
        seen["content_type"] = request.headers.get("content-type", "")
        seen["api_key"] = request.headers.get("xi-api-key")
        seen["body"] = request.read().decode("utf-8", "replace")
        return httpx.Response(200, json={"text": "我要找王偉麻煩一下", "language_code": "zho"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    c = SttClient(api_key="k", keyterms=["王偉", "汪偉"], http=http)

    out = c.transcribe(wav)

    assert isinstance(out, Transcript)
    assert out.text == "我要找王偉麻煩一下"
    assert out.language == "zho"
    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["api_key"] == "k"

    fields = _multipart_fields(seen["content_type"], seen["body"])
    names = [name for name, _ in fields]
    single = dict(fields)
    assert single["model_id"] == "scribe_v2"
    assert single["language_code"] == "zh"
    assert single["tag_audio_events"] == "false"
    assert single["num_speakers"] == "1"
    # keyterms 是重複的同名 part（multipart 陣列），不是逗號串或 JSON
    assert names.count("keyterms") == 2
    assert [v for name, v in fields if name == "keyterms"] == ["王偉", "汪偉"]
    assert single["file"] == "RIFF0000WAVE"
    assert 'filename="a.wav"' in seen["body"]


def test_uses_env_var_when_api_key_not_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "env-key")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    http = FakeHttp()

    SttClient(http=http).transcribe(wav)

    assert http.calls[0]["headers"]["xi-api-key"] == "env-key"


def test_response_maps_words_and_raw(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    body = {"text": "王偉", "language_code": "zho", "words": [{"text": "王偉", "start": 0.1}]}
    out = SttClient(api_key="k", http=FakeHttp(FakeResp(body))).transcribe(wav)

    assert out.words == [{"text": "王偉", "start": 0.1}]
    assert out.raw is body


def test_401_raises_auth_error_with_key_hint(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    resp = FakeResp({"detail": "invalid api key"}, status_code=401, text='{"detail":"invalid api key"}')
    c = SttClient(api_key="bad", http=FakeHttp(resp))

    with pytest.raises(SttAuthError) as exc:
        c.transcribe(wav)

    msg = str(exc.value)
    assert "ELEVENLABS_API_KEY" in msg
    assert "重試" in msg
    assert exc.value.status_code == 401
    assert isinstance(exc.value, SttError)


def test_422_raises_request_error_with_multipart_hint(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    resp = FakeResp({"detail": "unprocessable"}, status_code=422, text='{"detail":"unprocessable"}')
    c = SttClient(api_key="k", keyterms=["王偉"], http=FakeHttp(resp))

    with pytest.raises(SttRequestError) as exc:
        c.transcribe(wav)

    msg = str(exc.value)
    assert "multipart" in msg
    assert "keyterms" in msg
    assert exc.value.status_code == 422


def test_500_keeps_body_snippet(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    c = SttClient(api_key="k", http=FakeHttp(FakeResp({}, status_code=500, text="upstream boom")))

    with pytest.raises(SttError) as exc:
        c.transcribe(wav)

    assert not isinstance(exc.value, (SttAuthError, SttRequestError))
    assert "upstream boom" in str(exc.value)


def test_httpx_not_imported_when_http_injected():
    """`http=` 注入時不得 import httpx（計畫 A2：只有真要打 API 才 lazy import）。"""
    code = (
        "import sys; from dialname.stt_client import SttClient;"
        "SttClient(api_key='k', http=object());"
        "print('httpx' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
