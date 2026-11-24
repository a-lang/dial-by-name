# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/calls.py replay`（L2）：wav → 假 STT → handle_turn 的兩層判定。

規格見 `docs/design.md` §回歸三層：決策層＝新決策 vs `expect`；
STT 層＝新逐字稿 vs 劇本台詞（先過 `core.normalize`），同音／近音標「STT 變體」
並分類成人名同音／動詞殘骸／漏部門／贅詞。429 要退避重試、仍失敗記 stt-error
且不中斷整場；沒有 key 直接停下，不造假逐字稿。

這裡全部用假 STT（不打網路），並以**程式呼叫**而非 subprocess 跑 replay，
才能注入 `stt_factory` 與 `sleep`（429 退避不會真的睡）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dialname.core import load_directory
from dialname.stt_client import SttError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = json.loads((ROOT / "data" / "call_script.json").read_text(encoding="utf-8"))
AUTO = [case for case in SCRIPT["calls"] if case["mode"] == "auto"]
TURNS = sum(len(case["turns"]) for case in AUTO)          # 24：18 通、6 通兩輪


def _load_calls_module():
    # dataclass 需要模組先進 sys.modules，否則 cls.__module__ 找不到。
    spec = importlib.util.spec_from_file_location("calls_replay", ROOT / "scripts" / "calls.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calls = _load_calls_module()


@pytest.fixture(scope="module")
def directory():
    return load_directory(ROOT / "data" / "people.json")


# --------------------------------------------------------------------------- #
# 場次日誌與假 STT
# --------------------------------------------------------------------------- #
def _record(call_id: str, turn: int, text: str, decision: dict) -> dict:
    return {
        "ts": f"2026-11-19T10:{turn:02d}:00+08:00",
        "call_id": call_id,
        "attempt": turn,
        "wav": f"/var/lib/asterisk/dial-by-name/recordings/{call_id}-{turn}.wav",
        "wav_duration_ms": 3000,
        "stt": {"text": text, "lang": "zho", "ms": 800},
        "candidates": [],
        "decision": decision,
        "stt_status": "ok",
        "decide_ms": 1,
    }


def build_session(path: Path) -> dict[str, str]:
    """18 通 auto 的完整有序日誌（＋1 通 manual 空白）；回傳 {wav 檔名: 劇本台詞}。"""
    lines: list[dict] = []
    texts: dict[str, str] = {}
    for index, case in enumerate(SCRIPT["calls"], start=1):
        if case["mode"] != "auto":
            continue
        call_id = f"fake-{index:02d}"
        for turn, text in enumerate(case["turns"], start=1):
            last = turn == len(case["turns"])
            expect = case["expect"]
            decision = {
                "action": expect["action"] if last else "ask",
                "target": expect["target"] if last else None,
                "reason": expect["reason"] if last else "clarify",
                "confidence": 0.9,
                "speech": [],
            }
            record = _record(call_id, turn, text, decision)
            lines.append(record)
            texts[Path(record["wav"]).name] = text
    # 一通 manual（沉默）的空白輪：不該被重播，只驗證它被跳過、不拖垮對齊。
    lines.append(_record("fake-19", 1, "", {"action": "retry", "target": None,
                                             "reason": "stt-empty", "confidence": None,
                                             "speech": []}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in lines),
        encoding="utf-8",
    )
    return texts


class FakeStt:
    """依 wav 檔名回固定逐字稿；可指定某些檔名丟錯（模擬 429）或先空一次。"""

    def __init__(self, texts: dict[str, str], fail: dict[str, list[Exception]] | None = None,
                 empty_once: set[str] | None = None):
        self.texts = texts
        self.fail = {k: list(v) for k, v in (fail or {}).items()}
        self.empty_once = set(empty_once or ())
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        name = Path(path).name
        pending = self.fail.get(name)
        if pending:
            raise pending.pop(0)
        if name in self.empty_once:
            self.empty_once.discard(name)
            return SimpleNamespace(text="", language="zho")
        return SimpleNamespace(text=self.texts.get(name, ""), language="zho")


def _replay(tmp_path, directory, texts, **kwargs):
    log = tmp_path / "session" / "20261119.jsonl"
    build_session(log)
    out = tmp_path / "replay.json"
    code = calls.replay(
        SCRIPT, directory, log,
        stt_factory=kwargs.pop("stt_factory", lambda _terms: FakeStt(texts)),
        pause=0,
        sleep=lambda _s: None,
        out_path=out,
        **kwargs,
    )
    return code, out


def _texts(tmp_path) -> dict[str, str]:
    return build_session(tmp_path / "probe" / "probe.jsonl")


# --------------------------------------------------------------------------- #
# 分類函式（報告與測試共用的那一套）
# --------------------------------------------------------------------------- #
def test_classify_variant_kinds(directory):
    classify = calls.classify_stt_variant
    assert classify("我要找鄭博文", "呃，我要找郑博文。", directory) == "贅詞"
    assert classify("我要找汪偉", "我要找汪维。", directory) == "人名同音"
    assert classify("小美", "呃，小米。", directory) == "人名同音"
    assert classify("資訊部王偉", "呃，王伟。", directory) == "漏部門"
    assert classify("幫我接業務部", "呃，报街业务部。", directory) == "動詞殘骸"
    assert classify("我要找鄭博文", "我要找鄭博文", directory) == ""
    assert classify("我要找鄭博文", "", directory) == "空逐字稿"


def test_resolve_wav_falls_back_to_local_recordings(tmp_path):
    session = tmp_path / "session"
    local = session / "recordings"
    local.mkdir(parents=True)
    (local / "abc-1.wav").write_bytes(b"RIFF")
    resolved = calls._resolve_wav("/var/lib/asterisk/dial-by-name/recordings/abc-1.wav", session)
    assert resolved == local / "abc-1.wav"


# --------------------------------------------------------------------------- #
# 全綠基線
# --------------------------------------------------------------------------- #
def test_replay_all_consistent(tmp_path, directory, capsys):
    code, out = _replay(tmp_path, directory, _texts(tmp_path))

    stdout = capsys.readouterr().out
    assert code == 0
    assert "18 通 auto：決策一致 18、漂移 0、未判定 0、未跑到 0" in stdout
    assert f"STT 層 {TURNS} 輪：逐字稿相同 {TURNS}、STT 變體 0、STT 失敗 0" in stdout
    assert "2 通 manual：跳過" in stdout
    assert "劇本台詞" in stdout and "新逐字稿" in stdout and "判定" in stdout
    assert "homophone-dept-carry" in stdout

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ab"] is False
    arm = data["arms"][0]
    assert arm["summary"]["consistent"] == 18
    assert arm["summary"]["turns"] == TURNS
    assert all(row["verdict"] == "通過" for row in arm["rows"])


# --------------------------------------------------------------------------- #
# STT 變體與分類表
# --------------------------------------------------------------------------- #
def test_replay_variant_table_and_json(tmp_path, directory, capsys):
    texts = dict(_texts(tmp_path))
    texts["fake-02-1.wav"] = "我要找汪维。"          # 人名同音
    texts["fake-12-1.wav"] = "呃，小米。"            # 人名同音（近音，決策漂移）
    texts["fake-07-1.wav"] = "呃，报街业务部。"      # 動詞殘骸（決策漂移）
    texts["fake-16-1.wav"] = "呃，王伟。"            # 漏部門
    texts["fake-01-1.wav"] = "呃，我要找郑博文。"    # 贅詞
    code, out = _replay(tmp_path, directory, texts)

    stdout = capsys.readouterr().out
    assert "STT 錯誤分類（類型／案例／次數／決策後果" in stdout
    for kind in ("人名同音", "動詞殘骸", "漏部門", "贅詞"):
        assert kind in stdout
    assert "#12" in stdout
    assert "STT 變體分類：人名同音 2、動詞殘骸 1、漏部門 1、贅詞 1" in stdout

    data = json.loads(out.read_text(encoding="utf-8"))
    arm = data["arms"][0]
    assert arm["summary"]["variants"] == {
        "人名同音": 2, "動詞殘骸": 1, "漏部門": 1, "贅詞": 1,
    }
    # 變體造成決策漂移時照 expect 判漂移，不用「STT 變體」當出口。
    drift = [row["ci"] for row in arm["rows"] if row["verdict"] == "漂移"]
    assert 12 in drift and 7 in drift
    assert code == 1


# --------------------------------------------------------------------------- #
# 429 退避
# --------------------------------------------------------------------------- #
def test_replay_429_backoff_then_success(tmp_path, directory, capsys):
    texts = _texts(tmp_path)
    stt = FakeStt(texts, fail={"fake-16-1.wav": [SttError("429", 429)]})
    code, _out = _replay(tmp_path, directory, texts, stt_factory=lambda _terms: stt)

    stdout = capsys.readouterr().out
    assert code == 0
    assert "STT 失敗 0" in stdout
    assert "STT 呼叫失敗" not in stdout
    # 一次失敗＋重試成功：同一支 wav 打兩次，其餘照跑。
    assert stt.calls == TURNS + 1


def test_replay_429_exhausted_is_listed_not_fatal(tmp_path, directory, capsys):
    texts = _texts(tmp_path)
    stt = FakeStt(texts, fail={"fake-16-1.wav": [SttError("429", 429)] * 3})
    code, out = _replay(tmp_path, directory, texts, stt_factory=lambda _terms: stt)

    stdout = capsys.readouterr().out
    assert code == 1
    assert "STT 呼叫失敗（該通記 stt-error，不中斷整場）" in stdout
    assert "#16 homophone-second-turn 第 1 輪：429（退避 3 次仍失敗）" in stdout
    assert "18 通 auto：決策一致 17、漂移 0、未判定 1、未跑到 0" in stdout
    assert stt.calls == TURNS + (calls.REPLAY_429_TRIES - 1)

    data = json.loads(out.read_text(encoding="utf-8"))
    row = next(r for r in data["arms"][0]["rows"] if r["ci"] == 16)
    assert row["verdict"].startswith("未判定")
    assert row["errors"] == [{"turn": 1, "error": "429（退避 3 次仍失敗）"}]


def test_replay_empty_transcript_is_retried_once(tmp_path, directory, capsys):
    """Scribe 偶發回空：同一個 wav 再打一次，成功就不記空逐字稿。"""
    texts = _texts(tmp_path)
    stt = FakeStt(texts, empty_once={"fake-01-1.wav"})
    code, _out = _replay(tmp_path, directory, texts, stt_factory=lambda _terms: stt)

    stdout = capsys.readouterr().out
    assert code == 0
    assert "空逐字稿" not in stdout
    assert "STT 變體 0" in stdout
    assert stt.calls == TURNS + 1


def test_replay_incomplete_call_is_undecided_not_drift(tmp_path, directory, capsys):
    """日誌只到第一輪、重播停在追問：不能算漂移（決策要到第二輪才有答案）。"""
    log = tmp_path / "session" / "20261119.jsonl"
    texts = build_session(log)
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    lines = [row for row in lines if not row["wav"].endswith("fake-12-2.wav")]
    log.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
                   encoding="utf-8")

    code = calls.replay(SCRIPT, directory, log, stt_factory=lambda _t: FakeStt(texts),
                        pause=0, sleep=lambda _s: None, out_path=tmp_path / "r.json")

    stdout = capsys.readouterr().out
    assert "未判定（只到 1/2 輪）" in stdout
    assert "#12 alias-homophone-cs" in stdout.split("未判定（不是通過")[1]
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    row = next(r for r in data["arms"][0]["rows"] if r["ci"] == 12)
    assert row["verdict"] == "未判定（只到 1/2 輪）"
    assert code == 1


# --------------------------------------------------------------------------- #
# --keyterms / --ab
# --------------------------------------------------------------------------- #
def test_replay_ab_compares_keyterms_arms(tmp_path, directory, capsys):
    texts = _texts(tmp_path)
    broken = dict(texts)
    broken["fake-12-1.wav"] = "呃，小米。"
    broken["fake-16-1.wav"] = "呃，王伟。"

    def factory(terms):
        return FakeStt(texts if terms else broken)   # B 臂：keyterms 救回小美

    code, out = _replay(tmp_path, directory, texts, stt_factory=factory, ab=True)

    stdout = capsys.readouterr().out
    assert code == 1
    assert "A（無 keyterms）" in stdout and "B（有 keyterms）" in stdout
    assert "A/B 對照（keyterms）" in stdout
    assert "人名同音錯聽次數" in stdout
    assert "決策一致率" in stdout

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ab"] is True
    a, b = data["arms"]
    assert a["summary"]["variants"].get("人名同音") == 1
    assert a["summary"]["variants"].get("漏部門") == 1
    assert a["summary"]["consistent"] < 18
    assert b["summary"]["variants"].get("人名同音", 0) == 0
    assert b["summary"]["consistent"] == 18
    assert b["keyterms"] > 0


def test_replay_without_key_stops(tmp_path, directory, monkeypatch, capsys):
    log = tmp_path / "session" / "20261119.jsonl"
    build_session(log)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(calls, "ENV_PATH", tmp_path / "no-such.env")
    monkeypatch.setattr(calls, "REPLAY_DIR", tmp_path / "out")

    code = calls.main(["replay", str(log)])

    captured = capsys.readouterr()
    assert code == 2
    assert "ELEVENLABS_API_KEY 未設定" in captured.err
    assert not (tmp_path / "out").exists()
