#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""模糊拉鋸（(d) 分支）可達面掃描：`docs/decision-logic.md` §6（外部模型）。

量「現行規則下，`decide()` 的 (d) 分支（本地收斂不了的多候選）在語料裡的可達率」。
分類不重現分支順序：語句以 `handle_turn`（生產同路徑，含候選 carry）跑過一遍，
reason 映射 L1–L4；`ambiguous-ask:*` 出現即 (d) 可達＝重開外部模型議題的訊號（計畫 §3）。

為什麼不用 attempt=2 的 reason：`alias-ask`／`homophone-sibling-ask` 在 attempt≥2
都塌縮成 `clarify-exhausted`（`decision.py` 四處共用 `exhausted()`），讀不出案例類型；
attempt=1 的 reason 是離散的（2026-11-21 以 `trace.py` 實測，計畫 §0.2-4）。

L1 本地已處置（轉接或總機）；L2 無候選（(d) 之前攔走）；
L3 設計上不外包（同音／alias／缺名，排在 (d) 之前）；L4 (d) 可達（重開訊號）；
L0 未映射的 reason（顯性列出，不靜默吞——新 reason 演進時要先補映射表）。

    uv run scripts/jev_probe.py scan                        # 342 合成＋L1 劇本
    uv run scripts/jev_probe.py scan --calls out/calls      # 加真實通話（people.json 冊時期）
    uv run scripts/jev_probe.py scan --pbx-db --source-directory 'Test Directory' \
        --calls out/calls-test                              # userman 冊時期（DB 需可達）
    uv run scripts/jev_probe.py scan --out out/jev/reachability.json

`--calls` 的 JSONL 必須與名冊**同期**：people.json 名冊＝`out/calls/`；userman 名冊的
`out/calls-test/` 要用 `--pbx-db --source-directory` 載入——名冊不同，L4 判讀沒有意義。
`--pbx-db` 沿用 `agi_server.py` 的連線規則（`.env` 四鍵齊備優先，缺才 fallback
`/etc/freepbx.conf`）；DB 不可達＝拒絕執行（不以空名冊繼續）。

2026-11-21：Jev 委派機制整個移除，`no-jev:*` 改名 `ambiguous-ask:*`；
分類語意不變，改動前後的分佈可直接對照。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
# `uv run scripts/jev_probe.py` 會把 scripts/ 放進 sys.path[0]；補回專案根目錄才 import 到套件。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialname.agi import handle_turn  # noqa: E402
from dialname.core import load_directory  # noqa: E402
from dialname.pbx_directory import (  # noqa: E402
    DEFAULT_ALIASES_PATH,
    DEFAULT_DEPARTMENTS_PATH,
    load_from_pbx,
    resolve_db_config,
)

DATA_PATH = ROOT / "data" / "people.json"
SYNTHETIC_PATH = ROOT / "out" / "eval_decisions.json"
SCRIPT_PATH = ROOT / "data" / "call_script.json"
OUT_PATH = ROOT / "out" / "jev" / "reachability.json"
ALIASES_PATH = ROOT / DEFAULT_ALIASES_PATH
DEPARTMENTS_PATH = ROOT / DEFAULT_DEPARTMENTS_PATH

# 只需讓 handle_turn 的 _wav_duration_ms() 回 None（同 scripts/calls.py 的手法）。
WAV = "/tmp/does-not-exist.wav"

KLASSES = ("L1", "L2", "L3", "L4", "L0-unknown")


class FakeStt:
    """只回固定逐字稿；不碰音檔、不碰網路。"""

    def __init__(self, text: str):
        self.text = text

    def transcribe(self, path):
        return SimpleNamespace(text=self.text, language="zho")


def classify(reason: str) -> str:
    """`decide()` 的 reason → L1–L4（映射表與計畫 §5 一致；未列→L0 顯性列出）。"""
    r = reason.split("(", 1)[0]
    if r.startswith("ambiguous-ask:"):
        return "L4"          # (d) 閘門：本地收斂不了的多候選 → 若重開外部模型就在這裡
    if r.startswith("direct:") or r in ("dept-only", "single-candidate",
                                       "low-confidence-single"):
        return "L1"          # 本地已處置（route／route_dept／floor 以下的 fallback）
    if r.startswith("no-candidate") or r in ("no-intent", "alias-not-found"):
        return "L2"          # 無候選；(d) 之前就攔走
    if r.startswith("homophone") or r in ("alias-ask", "incomplete-name",
                                         "surname-only", "dept-scoped-ask"):
        return "L3"          # 設計上不在本地猜的分支（排在其前）
    return "L0-unknown"


# --------------------------------------------------------------------------- #
# 語料載入：統一成 {"id", "kind", "file"?, "texts": [(attempt, text), ...]}
# --------------------------------------------------------------------------- #
def load_synthetic_queries(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"找不到合成題決策輸出：{path}（先跑 uv run eval.py）")
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        {"id": f"s{i:03d}", "kind": r.get("kind", ""),
         "texts": [(1, r["query"])]}
        for i, r in enumerate(rows, start=1)
    ]


def load_call_turns(path: Path) -> list[dict]:
    """JSONL → 每通（call_id）取第一輪與第 2 輪的第一個非空逐字稿（retry 的空行跳過）。"""
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    items = []
    for f in files:
        calls: dict[str, dict[int, str]] = {}
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            attempt = r.get("attempt") or 0
            text = ((r.get("stt") or {}).get("text") or "").strip()
            if attempt in (1, 2) and text:
                calls.setdefault(r.get("call_id") or f.name, {}) \
                     .setdefault(attempt, text)
        for call_id, first in calls.items():
            texts = [(a, first[a]) for a in (1, 2) if a in first]
            if texts:
                items.append({"id": call_id, "kind": "call", "file": f.name,
                              "texts": texts})
    return items


def load_script_turns(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"找不到 L1 劇本：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for key in ("calls", "l1_only"):
        for case in data.get(key, []):
            texts = [(a, t.strip()) for a, t in enumerate(case.get("turns", []), 1)
                     if t.strip()]
            if texts:
                items.append({"id": case.get("id", ""), "kind": case.get("kind", ""),
                              "texts": texts})
    return items


# --------------------------------------------------------------------------- #
# 掃描引擎：handle_turn（生產同路徑）＋ reason 映射
# --------------------------------------------------------------------------- #
def run_item(directory, item: dict) -> list[dict]:
    """跑完一個案例的每一輪（載入層已保證逐字稿非空）。

    下一輪的候選 carry 用上一輪 `handle_turn` 的回傳值（同 scripts/calls.py run_case）。
    """
    rows, prev = [], None
    for attempt, text in item["texts"]:
        result = handle_turn(WAV, 1, stt=FakeStt(text), directory=directory,
                             prev_pids=prev)
        reason = result.get("reason", "")
        rows.append({
            "id": item.get("id", ""), "kind": item.get("kind", ""),
            "file": item.get("file"), "turn": attempt, "text": text,
            "reason": reason.split("(", 1)[0], "action": result.get("action", ""),
            "klass": classify(reason),
        })
        prev = result.get("cands")
    return rows


def load_roster(args):
    """名冊入口：JSON（PoC／回滾）或 FreePBX userman（`--pbx-db`）。

    與 `agi_server.py` 的兩種模式一一對應——掃描必須跑在**同一份名冊**上，
    否則 `--calls` 的 L4 判讀會對錯人（userman 時期的通話不能拿 people.json 掃）。
    """
    if args.pbx_db:
        try:
            db = resolve_db_config()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        return load_from_pbx(
            args.source_directory,
            db=db,
            departments_path=args.departments,
            aliases_path=args.aliases,
        )
    return load_directory(args.data)


def scan(args) -> int:
    directory = load_roster(args)
    corpora = {
        "synthetic": load_synthetic_queries(Path(args.synthetic)),
        "calls": ([] if not args.calls
                  else [it for p in args.calls for it in load_call_turns(Path(p))]),
        "script": load_script_turns(Path(args.script)),
    }

    rows, dist = [], {}
    for source, items in corpora.items():
        for item in items:
            item_rows = run_item(directory, item)
            rows.extend(item_rows)
            for r in item_rows:
                dist.setdefault(f"{source}|{r['turn']}", Counter())[r["klass"]] += 1

    l4 = [r for r in rows if r["klass"] == "L4"]
    l0 = [r for r in rows if r["klass"] == "L0-unknown"]
    out = {
        "meta": {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "roster": (f"pbx:{args.source_directory}" if args.pbx_db
                       else str(args.data)),
            "synthetic": str(args.synthetic),
            "calls": [str(p) for p in (args.calls or [])],
            "script": str(args.script),
            "note": "L4>0＝重開外部模型議題的訊號（docs/decision-logic.md §6）；"
                    "分類由現行規則定義，規則／門檻／名冊變更後須重跑。",
        },
        "distribution": {k: {kk: c.get(kk, 0) for kk in KLASSES}
                         for k, c in sorted(dist.items())},
        "l4": l4,
        "rows": rows,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("模糊拉鋸可達面掃描（(d) 分支＝本地收斂不了的多候選；L4>0＝重開訊號）")
    print(f"{'語料':10s} {'輪':>2s} " + " ".join(f"{k:>10s}" for k in KLASSES) + "  總數")
    for key, c in sorted(dist.items()):
        source, turn = key.split("|")
        total = sum(c.values())
        print(f"{source:10s} {turn:>2s} "
              + " ".join(f"{c.get(k, 0):>10d}" for k in KLASSES)
              + f"  {total}")
    if l4:
        print(f"\n*** 重開訊號：L4 共 {len(l4)} 題（ambiguous-ask:*＝本地收斂不了的拉鋸）***")
        for r in l4:
            print(f"    [{r['klass']}] {r['reason']:22s} {r['text']}")
    else:
        print(f"\n重開訊號（L4＝(d) 可達）：0 題")
    if l0:
        print(f"\n未映射 reason（L0，須補映射表）：{len(l0)} 題")
        for r in l0:
            print(f"    {r['reason']:24s} {r['text']}")
    print(f"\nwrote {out_path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="模糊拉鋸可達面掃描（docs/decision-logic.md §6）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    scan_ap = sub.add_parser("scan", help="掃語料的 (d) 分支可達率（L1–L4 分佈）")
    scan_ap.add_argument("--data", default=str(DATA_PATH), help="名冊 JSON（未用 --pbx-db 時）")
    scan_ap.add_argument("--pbx-db", action="store_true",
                         help="名冊改讀 FreePBX userman（需 --source-directory；30 通評測用）")
    scan_ap.add_argument("--source-directory",
                         help="userman_directories.name（--pbx-db 必填，例：'Test Directory'）")
    scan_ap.add_argument("--aliases", default=str(ALIASES_PATH),
                         help=f"aliases.json（僅 --pbx-db；預設 {ALIASES_PATH}）")
    scan_ap.add_argument("--departments", default=str(DEPARTMENTS_PATH),
                         help=f"部門定義（僅 --pbx-db；預設 {DEPARTMENTS_PATH}）")
    scan_ap.add_argument("--synthetic", default=str(SYNTHETIC_PATH),
                         help="合成題 queries（eval_decisions.json；先跑 eval.py）")
    scan_ap.add_argument("--calls", nargs="+", default=None,
                         help="實撥 JSONL 檔或目錄（須與名冊同期；userman 冊搭 --pbx-db）")
    scan_ap.add_argument("--script", default=str(SCRIPT_PATH), help="L1 劇本 JSON")
    scan_ap.add_argument("--out", default=str(OUT_PATH), help="輸出 JSON 路徑")
    scan_ap.set_defaults(func=scan)

    args = ap.parse_args(argv)
    if args.cmd == "scan":
        if args.pbx_db and not args.source_directory:
            ap.error("--pbx-db 需要 --source-directory <userman_directories.name>")
        if args.source_directory and not args.pbx_db:
            ap.error("--source-directory 僅適用 --pbx-db")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
