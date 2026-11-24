#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""檢查 sounds/zh 的 19 個提示音：檔名齊全、純 ASCII、8 kHz／mono／16-bit。

用法：
    uv run scripts/check_sounds.py [sounds/zh]
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

EXPECTED = [
    "welcome",
    "processing",
    "retry",
    "q-say-full-name",
    "q-which-dept",
    "q-dept-who",
    "q-operator",
    "transferring",
    "to-operator",
    "not-found",
    "goodbye",
    "dept-sales",
    "dept-cs",
    "dept-rd",
    "dept-fin",
    "dept-wh",
    "dept-adm",
    "dept-hr",
    "dept-it",
]


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "sounds/zh")
    problems: list[str] = []
    for name in EXPECTED:
        path = root / f"{name}.wav"
        if not path.exists():
            problems.append(f"缺少 {path}")
            continue
        if not path.name.isascii():
            problems.append(f"檔名非 ASCII：{path.name}")
        try:
            with wave.open(str(path), "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (8000, 1, 2):
                    problems.append(
                        f"{path.name}: 格式 {w.getframerate()}Hz/{w.getnchannels()}ch/"
                        f"{w.getsampwidth() * 8}bit（要 8000/1/16）"
                    )
        except wave.Error as exc:
            problems.append(f"{path.name}: 不是合法 wav（{exc}）")

    for extra in sorted(root.glob("*.wav")):
        if extra.stem not in EXPECTED:
            print(f"  [warn] 非預期音檔：{extra.name}（確認不是含姓名的 name-*.wav）")

    if problems:
        print("FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: {len(EXPECTED)} 個音檔齊全且格式正確（{root}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
