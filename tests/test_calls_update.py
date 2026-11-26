# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/calls.py update`：刻意變更時把 L1 的 expect 搬到新行為。

規格見 `docs/evaluation.md#regression`（update 重寫 expect 並列 diff）與
刻意變更政策（同節）。範圍只含 18 通 auto＋3 個 `l1_only`；台詞（turns）、truth、
kind、mode 一律不動，manual 案例（truth 為 null）不跑。

全部用假 STT（`run_case` 內建 `FakeStt`），不打網路。測試只在 tmp 檔上寫入；
唯一碰真檔的是 CLI `--dry-run`，並斷言位元不變。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dialname.core import load_directory

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "data" / "call_script.json"
CALLS_PY = ROOT / "scripts" / "calls.py"
ORIGINAL = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))


def _load_calls_module():
    # dataclass 需要模組先進 sys.modules，否則 cls.__module__ 找不到。
    spec = importlib.util.spec_from_file_location("calls_update", CALLS_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calls = _load_calls_module()


@pytest.fixture(scope="module")
def directory():
    return load_directory(ROOT / "data" / "people.json")


def _copy_script(tmp_path: Path) -> Path:
    path = tmp_path / "call_script.json"
    path.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _case(script: dict, case_id: str) -> dict:
    for case in script["calls"] + script["l1_only"]:
        if case["id"] == case_id:
            return case
    raise AssertionError(f"劇本沒有 {case_id}")


def _mutate(path: Path, case_id: str, expect: dict) -> None:
    """把某通的 expect 改成指定值（模擬漂移：舊 expect 尚未搬到新行為）。"""
    script = json.loads(path.read_text(encoding="utf-8"))
    _case(script, case_id)["expect"] = expect
    path.write_text(json.dumps(script, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def test_update_keeps_file_when_expect_matches(tmp_path, directory, capsys):
    """expect 與實算一致時：0 通搬移，檔案位元不變。"""
    path = _copy_script(tmp_path)
    before = path.read_bytes()

    assert calls.update(calls.load_script(path), directory, path) == 0

    out = capsys.readouterr().out
    assert "0 通搬移" in out
    assert "21 通不變" in out
    assert path.read_bytes() == before


def test_update_moves_drift_and_prints_diff(tmp_path, directory, capsys):
    """構造漂移 → 列該通 diff、寫回正確值，其他欄位與格式不動。"""
    path = _copy_script(tmp_path)
    _mutate(path, "exact-sc", {
        "action": "fallback", "reason": "surname-only", "target": None,
        "scores": [0.5],
    })

    assert calls.update(calls.load_script(path), directory, path) == 0

    out = capsys.readouterr().out
    assert "exact-sc" in out
    assert "→" in out
    assert "1 通搬移" in out
    assert "20 通不變" in out

    script = json.loads(path.read_text(encoding="utf-8"))
    case = _case(script, "exact-sc")
    want = _case(ORIGINAL, "exact-sc")
    assert case["expect"] == want["expect"]
    for field in ("kind", "mode", "turns", "truth"):
        assert case[field] == want[field]
    # 其他通的 expect 沒被順手改到。
    for case_id in ("homophone-dept-carry", "second-turn-empty-retry"):
        assert _case(script, case_id) == _case(ORIGINAL, case_id)

    # 寫回的格式與原檔一致（diff 只會出現在 expect 那幾行）。
    assert path.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")


def test_update_dry_run_does_not_write(tmp_path, directory, capsys):
    """--dry-run 只印 diff，檔案保持漂移狀態。"""
    path = _copy_script(tmp_path)
    _mutate(path, "exact-sc", {
        "action": "fallback", "reason": "surname-only", "target": None,
        "scores": [0.5],
    })
    before = path.read_bytes()

    assert calls.update(calls.load_script(path), directory, path, dry_run=True) == 0

    out = capsys.readouterr().out
    assert "exact-sc" in out
    assert "1 通搬移" in out
    assert "--dry-run" in out
    assert path.read_bytes() == before


def test_update_is_idempotent(tmp_path, directory, capsys):
    """連跑兩次：第二次 0 通搬移，檔案位元相同。"""
    path = _copy_script(tmp_path)
    _mutate(path, "exact-sc", {
        "action": "fallback", "reason": "surname-only", "target": None,
        "scores": [0.5],
    })

    calls.update(calls.load_script(path), directory, path)
    capsys.readouterr()
    first = path.read_bytes()

    assert calls.update(calls.load_script(path), directory, path) == 0
    out = capsys.readouterr().out
    assert "0 通搬移" in out
    assert path.read_bytes() == first


def test_update_leaves_manual_cases_alone(tmp_path, directory, capsys):
    """manual（truth 為 null）不跑、不改：expect 保持原樣，truth 仍是 null。"""
    path = _copy_script(tmp_path)
    drifted = {"action": "retry", "reason": "stt-empty", "target": None}
    _mutate(path, "silence", drifted)

    assert calls.update(calls.load_script(path), directory, path) == 0

    out = capsys.readouterr().out
    assert "0 通搬移" in out
    assert "manual（不跑" in out

    script = json.loads(path.read_text(encoding="utf-8"))
    silence = _case(script, "silence")
    assert silence["expect"] == drifted
    assert silence["truth"] is None
    assert silence["turns"] == []


def test_cli_dry_run_keeps_repo_file_intact():
    """CLI 的 --dry-run 有接上，且不會寫真檔（基準 expect 已同步）。"""
    before = SCRIPT_PATH.read_bytes()
    proc = subprocess.run(
        [sys.executable, str(CALLS_PY), "update", "--dry-run"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    assert "0 通搬移" in proc.stdout
    assert SCRIPT_PATH.read_bytes() == before
