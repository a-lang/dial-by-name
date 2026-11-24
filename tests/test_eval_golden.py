# SPDX-License-Identifier: AGPL-3.0-or-later
"""L0：342 題合成測資對 golden 的回歸測試。

三層斷言（見 `docs/design.md` §回歸三層）：
  1. 16 項指標 dict 與 `tests/golden/eval_summary.json` 完全相等；
  2. 決策層逐題 hash 與 `tests/golden/eval_rows_sha256.txt` 相等（不符 fail）；
  3. 分數層逐題 hash 與 `tests/golden/eval_scores_sha256.txt` 相等（不符 warning）。

hash 檔第一行是 `# sha256 <hex>`，其後是參與 hash 的原始行（TSV）。
保留原始行是為了 hash 不符時能指出**哪幾題**變了，而不是只丟兩個 hex。
寫入與斷言共用 `digest_lines()`，不存在兩套 hash 邏輯。

刻意改行為後重算（不重算 eval_summary.json）：

    uv run python tests/test_eval_golden.py --write
"""

from __future__ import annotations

import hashlib
import json
import sys
import warnings
from collections import Counter
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
SUMMARY_PATH = GOLDEN_DIR / "eval_summary.json"
ROWS_HASH_PATH = GOLDEN_DIR / "eval_rows_sha256.txt"
SCORES_HASH_PATH = GOLDEN_DIR / "eval_scores_sha256.txt"

sys.path.insert(0, str(ROOT))

import eval as eval_mod  # noqa: E402
from dialname.core import load_directory  # noqa: E402

DECISION_FIELDS = ("query", "kind", "action", "reason", "action2")


# ── 同源：測試與 --write 都走這裡 ────────────────────────────────────────


@lru_cache(maxsize=1)
def _run_once() -> tuple[list[dict], list[dict], dict]:
    directory = load_directory(ROOT / "data" / "people.json")
    people, departments = directory.people, directory.departments
    cases = eval_mod.build_cases(people, departments)
    rows = eval_mod.run(cases, directory, departments)
    dec_rows = eval_mod.run_decisions(cases, directory, departments)
    summary = eval_mod.summarize(rows, dec_rows)
    return rows, dec_rows, summary


def _cell(row: dict, field: str) -> str:
    value = row.get(field)
    return "" if value is None else str(value)


def decision_lines(dec_rows: list[dict]) -> list[str]:
    """依 query 排序後串成 `query\\tkind\\taction\\treason\\taction2`。"""
    ordered = sorted(dec_rows, key=lambda r: tuple(_cell(r, f) for f in DECISION_FIELDS))
    return ["\t".join(_cell(r, f) for f in DECISION_FIELDS) for r in ordered]


def score_lines(rows: list[dict]) -> list[str]:
    """依 query 排序後串成 `query\\ttop1`（分數，三位小數）。"""
    lines = [f"{r['query']}\t{r['top1_score']:.3f}" for r in rows]
    return sorted(lines, key=lambda line: line.split("\t", 1)[0])


def digest_lines(lines: list[str]) -> str:
    payload = "".join(line + "\n" for line in lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _query_of(line: str) -> str:
    return line.split("\t", 1)[0]


def _diff_queries(want: list[str], got: list[str]) -> list[str]:
    """回傳內容不同的 query（含重複 query，以 multiset 比對）。"""
    changed = Counter(got) - Counter(want)
    changed |= Counter(want) - Counter(got)
    return sorted({_query_of(line) for line in changed})


def _sample_diff(want: list[str], got: list[str], limit: int = 5) -> str:
    want_set, got_set = set(want), set(got)
    only_want = [line for line in want if line not in got_set][:limit]
    only_got = [line for line in got if line not in want_set][:limit]
    parts = []
    if only_want:
        parts.append("  基準有、現在沒有：\n" + "\n".join(f"    {line!r}" for line in only_want))
    if only_got:
        parts.append("  現在有、基準沒有：\n" + "\n".join(f"    {line!r}" for line in only_got))
    return "\n".join(parts)


def read_hash_file(path: Path) -> tuple[str, list[str]]:
    if not path.exists():
        raise AssertionError(
            f"找不到 golden：{path.relative_to(ROOT)}\n"
            "先產生基準：uv run python tests/test_eval_golden.py --write"
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("# sha256 "):
        raise AssertionError(f"{path.relative_to(ROOT)} 格式錯誤：第一行應為 '# sha256 <hex>'")
    return lines[0].split(maxsplit=2)[2], lines[1:]


def write_hash_file(path: Path, lines: list[str]) -> None:
    body = "".join(line + "\n" for line in lines)
    path.write_text(f"# sha256 {digest_lines(lines)}\n" + body, encoding="utf-8")


# ── 測試 ────────────────────────────────────────────────────────────────


def test_summary_matches_golden():
    _, _, summary = _run_once()
    golden = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    diff = {
        key: (golden.get(key), summary.get(key))
        for key in sorted(set(golden) | set(summary))
        if golden.get(key) != summary.get(key)
    }
    assert not diff, "342 指標與 golden 不符（golden, 現在）：\n" + "\n".join(
        f"  {key}: {old!r} -> {new!r}" for key, (old, new) in diff.items()
    )


def test_decision_rows_hash_matches_golden():
    _, dec_rows, _ = _run_once()
    want_hash, want_lines = read_hash_file(ROWS_HASH_PATH)
    got_lines = decision_lines(dec_rows)
    got_hash = digest_lines(got_lines)
    if got_hash != want_hash:
        queries = _diff_queries(want_lines, got_lines)
        raise AssertionError(
            "決策層逐題 hash 不符（L0）：指標可能沒動，但下列題目的決策換了。\n"
            f"  差異題數：{len(queries)}\n"
            f"  差異題目：{', '.join(queries[:40])}"
            f"{' …' if len(queries) > 40 else ''}\n"
            f"{_sample_diff(want_lines, got_lines)}\n"
            f"  golden: {want_hash}\n"
            f"  now   : {got_hash}"
        )


def test_score_rows_hash_matches_golden():
    rows, _, _ = _run_once()
    want_hash, want_lines = read_hash_file(SCORES_HASH_PATH)
    got_lines = score_lines(rows)
    got_hash = digest_lines(got_lines)
    if got_hash != want_hash:
        queries = _diff_queries(want_lines, got_lines)
        warnings.warn(
            "分數層逐題 hash 不符（L0，warning 不 fail）：決策沒翻，但 top1 分數位移。\n"
            f"  差異題數：{len(queries)}\n"
            f"  差異題目：{', '.join(queries[:40])}"
            f"{' …' if len(queries) > 40 else ''}\n"
            f"{_sample_diff(want_lines, got_lines)}\n"
            f"  golden: {want_hash}\n"
            f"  now   : {got_hash}",
            stacklevel=2,
        )


# ── 重算 hash（刻意改行為後才用） ────────────────────────────────────────


def _write() -> None:
    rows, dec_rows, _ = _run_once()
    write_hash_file(ROWS_HASH_PATH, decision_lines(dec_rows))
    write_hash_file(SCORES_HASH_PATH, score_lines(rows))
    print(f"wrote {ROWS_HASH_PATH.relative_to(ROOT)}")
    print(f"wrote {SCORES_HASH_PATH.relative_to(ROOT)}")
    print("提醒：eval_summary.json 未動；若指標也變了，另依 tests/golden/README.md 更新。")


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        _write()
    else:
        raise SystemExit("用法：uv run python tests/test_eval_golden.py --write")
