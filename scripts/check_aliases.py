#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`aliases.json` 唯讀檢查：孤兒／重複／空值／控制字元，一次全列。

IT 改完 sidecar 先跑這支，不必重啟服務；退出碼 0＝可以 reload。
只 import `dialname`（AGENTS.md 契約）：驗證規則與 `agi_server.py` 的載入完全相同，
所以「這裡綠」＝「reload 不會因 alias 失敗」。

    uv run scripts/check_aliases.py --source-directory 'PBX Internal Directory'
    uv run scripts/check_aliases.py --source-directory 'Test Directory' --aliases /tmp/aliases.json
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 直接執行 `scripts/check_aliases.py` 會落在系統 python（沒有 pymysql）——自動改用專案 venv 重跑。
VENV_PY = ROOT / ".venv" / "bin" / "python"
if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
    os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])
# `uv run scripts/check_aliases.py` 會把 scripts/ 放進 sys.path[0]；補回專案根目錄才 import 到套件。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialname.pbx_directory import (  # noqa: E402
    DEFAULT_ALIASES_PATH,
    DEFAULT_DEPARTMENTS_PATH,
    build_people,
    fetch_users,
    load_aliases,
    load_departments,
    resolve_db_config,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="aliases.json 唯讀檢查（不啟動服務、不改檔）")
    ap.add_argument("--source-directory", required=True, help="userman_directories.name")
    ap.add_argument("--aliases", default=DEFAULT_ALIASES_PATH, help=f"預設 {DEFAULT_ALIASES_PATH}")
    ap.add_argument("--departments", default=DEFAULT_DEPARTMENTS_PATH, help=f"預設 {DEFAULT_DEPARTMENTS_PATH}")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        db = resolve_db_config()
        departments = load_departments(args.departments)
        people = build_people(
            args.source_directory,
            users=fetch_users(db, args.source_directory),
            departments=departments,
        )
        mapping = load_aliases(
            args.aliases, pids=[p.pid for p in people], source_directory=args.source_directory
        )
    except (ValueError, RuntimeError) as exc:
        print(f"檢查失敗：\n{exc}", file=sys.stderr)
        return 1

    print(
        f"OK: source={args.source_directory!r} people={len(people)} "
        f"aliases={sum(len(v) for v in mapping.values())} people_with_alias={len(mapping)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
