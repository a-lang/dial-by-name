# SPDX-License-Identifier: AGPL-3.0-or-later
"""`scripts/jev_probe.py scan`：reason 映射、L4 fixture、語料管線（計畫 §5）。

L4 fixture 用「兩個非同音候選＋一句同時含兩個名字」構造 close-race（2026-11-21
實驗找出：`陳大同倪大同` 對 陳大同／倪大同 → `ambiguous-ask:close-race`）——這是唯一
能在現行規則下確定觸發 (d) 閘門的句型，也正是現實語料 L4=0 的原因。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("jev_probe", ROOT / "scripts" / "jev_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# L4 fixture 名冊：兩人名都不是同音（陳≠倪），查詢同時含兩名字 → close-race → (d)。
FIXTURE_ROSTER = {
    "source_directory": "Fix Dir",
    "departments": [
        {"dept_id": "sales", "name": "業務部", "aliases": ["業務"]},
        {"dept_id": "it", "name": "資訊部", "aliases": ["資訊", "IT"]},
    ],
    "people": [
        {"pid": "f001", "name": "陳大同", "directory": "Fix Dir", "dept": "業務部",
         "title": "專員", "extension": "9001", "last_name": "陳", "first_name": "大同"},
        {"pid": "f002", "name": "倪大同", "directory": "Fix Dir", "dept": "資訊部",
         "title": "專員", "extension": "9002", "last_name": "倪", "first_name": "大同"},
    ],
}


def _write_json(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.mark.parametrize(("reason", "klass"), [
    # L4：(d) 閘門（本地收斂不了的多候選）＝重開訊號
    ("ambiguous-ask:low-score", "L4"),
    ("ambiguous-ask:close-race", "L4"),
    # L1：本地已處置（轉接或 floor 以下的 fallback）
    ("direct:confident", "L1"),
    ("dept-only", "L1"),
    ("single-candidate", "L1"),
    ("low-confidence-single", "L1"),
    # L2：無候選，(d) 之前攔走
    ("no-intent", "L2"),
    ("no-candidate-retry", "L2"),
    ("no-candidate-giveup", "L2"),
    ("alias-not-found", "L2"),
    # L3：設計上不外包（同音／alias／缺名，排在 (d) 之前）
    ("homophone-sibling-ask", "L3"),
    ("homophone-unresolvable", "L3"),
    ("alias-ask", "L3"),
    ("incomplete-name", "L3"),
    ("surname-only", "L3"),
    ("dept-scoped-ask", "L3"),
    # L0：未映射——新 reason 演進時顯性列出，須補映射表
    ("clarify-exhausted", "L0-unknown"),
    ("made-up-reason", "L0-unknown"),
])
def test_classify_mapping(reason, klass):
    assert probe.classify(reason) == klass


def test_scan_l3_alias_and_homophone_on_real_roster():
    """§0.2-4 的實測案例：正式冊上 alias-ask／homophone-sibling-ask 都落 L3。"""
    directory = probe.load_directory(ROOT / "data" / "people.json")
    for text, reason in (("小美", "alias-ask"), ("汪偉", "homophone-sibling-ask")):
        rows = probe.run_item(directory, {"id": text, "kind": "probe",
                                         "texts": [(1, text)]})
        assert rows[0]["reason"] == reason
        assert rows[0]["klass"] == "L3"


def test_scan_l4_reachable_fixture(tmp_path):
    """close-race 構造可達 (d)：ambiguous-ask:close-race → L4（重開訊號的偵測路徑）。"""
    directory = probe.load_directory(
        _write_json(tmp_path / "roster.json", FIXTURE_ROSTER))
    rows = probe.run_item(directory, {"id": "l4", "kind": "probe",
                                      "texts": [(1, "陳大同倪大同")]})
    assert rows[0]["reason"] == "ambiguous-ask:close-race"
    assert rows[0]["klass"] == "L4"


def test_scan_carry_narrows_second_turn(tmp_path):
    """第 2 輪只講部門 → carry 收斂到該部門的人（L1），不落 L4。"""
    directory = probe.load_directory(
        _write_json(tmp_path / "roster.json", FIXTURE_ROSTER))
    rows = probe.run_item(directory, {
        "id": "carry", "kind": "probe",
        "texts": [(1, "陳大同倪大同"), (2, "業務部")],
    })
    assert [r["turn"] for r in rows] == [1, 2]
    assert rows[0]["klass"] == "L4"
    assert rows[1]["reason"] == "direct:confident"
    assert rows[1]["klass"] == "L1"


def test_probe_has_no_live_client():
    """掃描本期不實作 live（計畫 §1-3）：不得引入 jev_client。"""
    source = (ROOT / "scripts" / "jev_probe.py").read_text(encoding="utf-8")
    assert "jev_client" not in source
    assert "JevClient" not in source


def test_missing_synthetic_file_exits(tmp_path):
    with pytest.raises(SystemExit, match="eval.py"):
        probe.load_synthetic_queries(tmp_path / "nope.json")


def test_scan_cli_end_to_end(tmp_path):
    """scan 子命令全程：三種語料 → reachability.json；L4 只來自 close-race 構造。"""
    roster = _write_json(tmp_path / "roster.json", FIXTURE_ROSTER)
    synthetic = _write_json(tmp_path / "eval_decisions.json", [
        {"query": "陳大同", "kind": "exact-sc", "action": "route"},
        {"query": "陳大同倪大同", "kind": "probe", "action": "ask"},
    ])
    script = _write_json(tmp_path / "call_script.json", {
        "version": 1,
        "calls": [{"id": "c1", "kind": "probe", "turns": ["陳大同倪大同", "業務部"]}],
        "l1_only": [],
    })
    calls = tmp_path / "20261121.jsonl"
    calls.write_text(
        json.dumps({"call_id": "1.10", "attempt": 1,
                   "stt": {"text": "陳大同倪大同"}}, ensure_ascii=False) + "\n"
        + json.dumps({"call_id": "1.10", "attempt": 2,
                      "stt": {"text": ""}}, ensure_ascii=False) + "\n"   # retry 空行
        + json.dumps({"call_id": "1.10", "attempt": 2,
                      "stt": {"text": "資訊部"}}, ensure_ascii=False) + "\n"
        + json.dumps({"call_id": "1.11", "attempt": 1,
                      "stt": {"text": "陳大同"}}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    out = tmp_path / "reachability.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "jev_probe.py"), "scan",
         "--data", str(roster), "--synthetic", str(synthetic),
         "--script", str(script), "--calls", str(calls), "--out", str(out)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr

    data = json.loads(out.read_text(encoding="utf-8"))
    dist = data["distribution"]
    assert dist["synthetic|1"] == {"L1": 1, "L2": 0, "L3": 0, "L4": 1, "L0-unknown": 0}
    assert dist["script|1"]["L4"] == 1
    assert dist["script|2"]["L1"] == 1          # 業務部 → carry 收斂
    # calls：同一檔兩通各自分組；retry 的空 attempt-2 跳過，取第一個非空（資訊部）→ carry 收斂
    assert dist["calls|1"] == {"L1": 1, "L2": 0, "L3": 0, "L4": 1, "L0-unknown": 0}
    assert dist["calls|2"]["L1"] == 1
    assert len(data["l4"]) == 3                  # synthetic＋script＋calls 各一
    assert all(r["reason"] == "ambiguous-ask:close-race" for r in data["l4"])
    assert "重開訊號" in proc.stdout


# --------------------------------------------------------------------------- #
# userman 名冊（30 通真實評測的前置）：--pbx-db 走 load_from_pbx，不走 JSON
# --------------------------------------------------------------------------- #
def test_scan_pbx_db_uses_userman_roster(tmp_path, monkeypatch):
    """`--pbx-db --source-directory` → `resolve_db_config()` ＋ `load_from_pbx()`。"""
    roster = _write_json(tmp_path / "roster.json", FIXTURE_ROSTER)
    synthetic = _write_json(tmp_path / "eval_decisions.json",
                            [{"query": "陳大同", "kind": "exact-sc"}])
    script = _write_json(tmp_path / "call_script.json", {"calls": [], "l1_only": []})
    out = tmp_path / "reachability.json"
    seen = {}

    def fake_resolve():
        seen["resolved"] = True
        return "db-config-sentinel"

    def fake_load(source_directory, *, db, departments_path, aliases_path):
        seen.update(source=source_directory, db=db,
                    aliases=Path(aliases_path).name,
                    departments=Path(departments_path).name)
        return probe.load_directory(roster)

    monkeypatch.setattr(probe, "resolve_db_config", fake_resolve)
    monkeypatch.setattr(probe, "load_from_pbx", fake_load)

    rc = probe.main(["scan", "--pbx-db", "--source-directory", "Test Directory",
                     "--synthetic", str(synthetic), "--script", str(script),
                     "--out", str(out)])
    assert rc == 0
    assert seen["resolved"] is True
    assert seen["source"] == "Test Directory"
    assert seen["db"] == "db-config-sentinel"
    assert seen["aliases"] == "aliases.json"
    assert seen["departments"] == "departments.json"
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["roster"] == \
        "pbx:Test Directory"


def test_scan_pbx_db_requires_source_directory():
    with pytest.raises(SystemExit):
        probe.main(["scan", "--pbx-db"])


def test_scan_source_directory_requires_pbx_db():
    with pytest.raises(SystemExit):
        probe.main(["scan", "--source-directory", "Test Directory"])
