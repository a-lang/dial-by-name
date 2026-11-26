#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""文件連結唯讀檢查：相對路徑與錨點，一次全列。

文件重寫後常留下「檔名改了、連結沒改」與「章節搬了、錨點失效」兩種斷鏈。
這支掃 README／CONTRIBUTING／AGENTS／SECURITY／ROADMAP 與 `docs/**/*.md`，
解析 `[text](target)`：目標檔存在、`#anchor` 對得上 `<a id>` 或 GitHub 標題 slug。
零網路、不 import `dialname`、不寫檔；退出碼 0＝全部可達。

    uv run python scripts/check_docs_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = ("README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md", "ROADMAP.md")
SKIP_DIRS = {"_legacy", ".plan", ".agents", ".venv", ".git", "out"}
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ANCHOR_RE = re.compile(r"""<a\s+(?:id|name)\s*=\s*["']([^"']+)["']""", re.I)
LINK_IN_HEADING_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


def slug(text: str) -> str:
    """GitHub 標題錨點：小寫、去標點、空白→`-`，保留 CJK 與數字。"""
    text = LINK_IN_HEADING_RE.sub(r"\1", text).lower()
    text = re.sub(r"[^\w\- ]", "", text)  # \w 含 CJK／字母／數字／底線
    return text.replace(" ", "-")


def anchors_of(lines: list[str]) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        found.update(a.lower() for a in HTML_ANCHOR_RE.findall(line))
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if not m:
            continue
        base = slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        found.add(base if n == 0 else f"{base}-{n}")
    return found


def iter_files() -> list[Path]:
    files = [ROOT / name for name in TOP_FILES if (ROOT / name).is_file()]
    for path in sorted((ROOT / "docs").rglob("*.md")):
        if SKIP_DIRS.isdisjoint(path.relative_to(ROOT).parts):
            files.append(path)
    return files


def links_of(path: Path) -> list[tuple[int, str]]:
    """回傳 (行號, target)，略過 fenced code block 內的範例。"""
    links: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for _, raw in LINK_RE.findall(line):
            target = raw.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            target = target.split(" ", 1)[0]  # 去掉 `"title"`
            links.append((lineno, target))
    return links


def main(argv: list[str] | None = None) -> int:
    files = iter_files()
    cache: dict[Path, set[str]] = {}
    errors: list[str] = []
    checked = 0
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        for lineno, target in links_of(path):
            if target.startswith(EXTERNAL_PREFIXES) or target == "#":
                continue
            if target.startswith("#"):
                file_part, anchor = "", target[1:]
            else:
                file_part, _, anchor = target.partition("#")
                anchor = anchor.strip()
            checked += 1
            dest = path if not file_part else (path.parent / unquote(file_part)).resolve()
            if not dest.exists():
                errors.append(f"{rel}:{lineno} -> {target}（目標不存在：{file_part}）")
                continue
            if anchor and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = anchors_of(dest.read_text(encoding="utf-8").splitlines())
                if unquote(anchor).lower() not in cache[dest]:
                    errors.append(f"{rel}:{lineno} -> {target}（錨點不存在：#{anchor}）")

    for err in errors:
        print(err, file=sys.stderr)
    if errors:
        print(f"FAIL: {len(files)} files, {len(errors)} broken links（共檢查 {checked} 條）", file=sys.stderr)
        return 1
    print(f"OK: {len(files)} files, 0 broken links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
