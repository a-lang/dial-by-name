# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/calls.py stats`：場次 JSONL 的延遲與分佈。

`stt.ms` 用 nearest-rank（排序後第 ceil(pct/100 × n) 個），所以 n=5 的 p95
是最後一個值；null 不進百分位，另計。輸入沿用 `load_log()`（單檔或場次目錄）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS_PY = ROOT / "scripts" / "calls.py"


def _row(call_id: str, ts: str, attempt: int, seq: int,
         ms: int | None, action: str, status: str) -> dict:
    return {
        "ts": ts,
        "call_id": call_id,
        "attempt": attempt,
        "seq": seq,
        "wav": f"/var/lib/asterisk/dial-by-name/recordings/{call_id}-{seq}.wav",
        "stt": {"text": "我要找王偉" if ms is not None else "", "ms": ms},
        "decision": {"action": action},
        "stream": {"status": status},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CALLS_PY), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_stats_distribution(tmp_path):
    rows = [
        _row("a", "2026-11-24T10:00:01+08:00", 1, 1, 100, "route", "ok"),
        _row("a", "2026-11-24T10:00:02+08:00", 2, 2, 200, "route", "ok"),
        _row("b", "2026-11-24T10:00:03+08:00", 1, 1, 300, "fallback", "dropped"),
        _row("b", "2026-11-24T10:00:04+08:00", 1, 2, 400, "retry", "error"),
        _row("c", "2026-11-24T10:00:05+08:00", 2, 3, 1000, "ask", "ok"),
        _row("d", "2026-11-24T10:00:06+08:00", 1, 1, None, "hangup", "stub"),
    ]
    log = _write_jsonl(tmp_path / "20261124.jsonl", rows)

    result = _run("stats", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "calls: 4  turns: 6",
        "stt.ms: n=4 p50=200 p95=1000 max=1000 skipped=2",
        "action: ask=1 fallback=1 hangup=1 retry=1 route=2",
        "stream.status: dropped=1 error=1 ok=3 stub=1",
        "stream anomalies: 3",
        "attempt: 1=4 2=2",
        "seq: 1=3 2=2 3=1",
    ]


def test_stats_empty_log(tmp_path):
    log = _write_jsonl(tmp_path / "20261124.jsonl", [])

    result = _run("stats", str(log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "calls: 0  turns: 0",
        "stt.ms: n=0 p50=- p95=- max=- skipped=0",
        "action: -",
        "stream.status: -",
        "stream anomalies: 0",
        "attempt: -",
        "seq: -",
    ]
