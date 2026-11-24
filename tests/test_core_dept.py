# SPDX-License-Identifier: AGPL-3.0-or-later
"""部門解析與匹配：casefold、ASCII 整段比對、載入驗證。"""

from __future__ import annotations

import json

import pytest

from dialname.core import (
    Department,
    dept_match,
    load_directory,
    normalize,
    strip_noise,
)


def _depts() -> list[Department]:
    return [
        Department("sales", "業務部", ["業務", "Sales"]),
        Department("it", "資訊部", ["資訊", "IT", "MIS"]),
        Department("hr", "人資部", ["人資", "HR"]),
    ]


def test_normalize_folds_ascii_only():
    assert normalize("Sales") == normalize("sales") == "sales"
    assert normalize("IT") == "it"
    assert normalize("業務部") == "業務部"


def test_dept_match_ascii_aliases_use_whole_run():
    cases = {
        "我要找it": ("it", "我要找"),
        "找 sales": ("sales", "找"),
        "我要找IT部門": ("it", "我要找"),
    }
    for query, (dept_id, residual) in cases.items():
        d, rest = dept_match(normalize(query), _depts())
        assert d is not None and d.dept_id == dept_id, query
        assert rest == residual, query

    # 短英文別名不得在英文單字裡誤中
    for query in ("with", "quality", "threshold", "找Anita"):
        d, _ = dept_match(normalize(query), _depts())
        assert d is None, query


def test_dept_match_keeps_chinese_substring_behavior():
    d, rest = dept_match(normalize("我要談業務合作"), _depts())
    assert d is not None and d.dept_id == "sales"
    assert rest == "我要談合作"


def test_alias_may_contain_department_suffix():
    depts = [Department("it", "資訊部", ["IT", "IT部門"])]
    d, rest = dept_match(normalize("我要找IT部門"), depts)
    assert d is not None and d.dept_id == "it"
    assert rest == "我要找"


def _roster(**overrides):
    data = {
        "source_directory": "PBX Internal",
        "departments": [
            {"dept_id": "sales", "name": "業務部", "aliases": ["業務", "Sales"]},
            {"dept_id": "it", "name": "資訊部", "aliases": ["資訊", "IT"]},
        ],
        "people": [
            {
                "pid": "u001",
                "name": "王偉",
                "directory": "PBX Internal",
                "dept": "業務部",
                "title": "業務經理",
                "extension": "2101",
                "last_name": "王",
                "first_name": "偉",
            },
            {
                "pid": "u002",
                "name": "林小華",
                "directory": "PBX Internal",
                "dept": "",
                "extension": "2102",
                "last_name": "林",
                "first_name": "小華",
            },
        ],
    }
    data.update(overrides)
    return data


def _write(tmp_path, data) -> str:
    path = tmp_path / "people.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_load_directory_resolves_dept_id_and_allows_empty(tmp_path):
    d = load_directory(_write(tmp_path, _roster()))
    by_id = {p.pid: p for p in d.people}
    assert by_id["u001"].dept_id == "sales"
    assert by_id["u001"].dept_name == "業務部"
    assert by_id["u002"].dept_id == ""
    assert by_id["u002"].dept_name == ""


def test_search_alias_may_contain_department_suffix(tmp_path):
    # N1 regression：alias 本身含「部門」時，不能先被 strip_noise 吃掉
    roster = _roster(
        departments=[{"dept_id": "it", "name": "資訊部", "aliases": ["IT部門"]}],
        people=[],
    )
    d = load_directory(_write(tmp_path, roster))
    for query in ("IT部門", "我要找IT部門"):
        _, dept, residual = d.search(query)
        assert dept is not None and dept.dept_id == "it", query
        assert residual == "", (query, residual)


def test_load_directory_rejects_bad_input(tmp_path):
    bad_id = _roster(
        departments=[{"dept_id": "Sales", "name": "業務部", "aliases": []}]
    )
    with pytest.raises(ValueError, match="dept_id"):
        load_directory(_write(tmp_path, bad_id))

    dup_id = _roster(
        departments=[
            {"dept_id": "sales", "name": "業務部", "aliases": []},
            {"dept_id": "sales", "name": "客服部", "aliases": []},
        ]
    )
    with pytest.raises(ValueError, match="重複"):
        load_directory(_write(tmp_path, dup_id))

    wrong_dir = _roster()
    wrong_dir["people"][0]["directory"] = "Other"
    with pytest.raises(ValueError, match="directory"):
        load_directory(_write(tmp_path, wrong_dir))

    unknown_dept = _roster()
    unknown_dept["people"][0]["dept"] = "不存在部"
    with pytest.raises(ValueError, match="對不到任何部門"):
        load_directory(_write(tmp_path, unknown_dept))


def test_leading_interjection_is_stripped():
    """STT 常吐「呃／嗯／欸」開頭；沒剝掉會讓 NOISE_PREFIXES 的 startswith 全失效。

    2026-11-13 實機：逐字稿「呃，我要找古廷伟。」→ 0 個候選 → 轉總機；
    同一句去掉「呃」→ 古庭瑋 0.787、single-candidate、轉 4105。
    """
    for prefix in ("呃", "嗯", "欸", "哦", "呃，", "嗯，"):
        stripped, _ = strip_noise(normalize(prefix + "我要找古廷伟"))
        assert stripped == "古廷偉", prefix


def test_dept_match_falls_back_to_pinyin_for_retroflex():
    """STT 把「人資部」聽成「人之不」（zi→zhi、bu 同音）時，字面比對救不到。

    2026-11-14 實機：找李佳容（人資部），第二輪答「人資部」→ 逐字稿「人之不」
    → 沒有候選 → 轉總機。發音完全相同，所以用整串拼音做後援。
    """
    dept, rest = dept_match(normalize("人之不"), _depts())
    assert dept is not None and dept.dept_id == "hr"
    assert rest == ""


def test_dept_pinyin_fallback_leaves_person_names_alone():
    for query in ("王偉", "李佳容"):
        dept, rest = dept_match(normalize(query), _depts())
        assert dept is None, query
        assert rest == query


def _with_wang_xiaoming(tmp_path):
    roster = _roster()
    roster["people"].append({
        "pid": "u003", "name": "王小明", "directory": "PBX Internal",
        "dept": "業務部", "extension": "2103",
        "last_name": "王", "first_name": "小明",
    })
    return load_directory(_write(tmp_path, roster))


def test_same_length_different_pronunciation_is_not_a_candidate(tmp_path):
    """同姓＋同字數的結構分不該讓發音不同的人變成候選。

    2026-11-14 實機：「王大明」抓到 王小明 0.839（只有一個候選 → 直接轉錯人）。
    """
    d = _with_wang_xiaoming(tmp_path)
    assert d.search("王大明")[0] == []
    names = [c.person.name for c in d.search("王小明")[0]]
    assert names == ["王小明"]


def test_same_length_homophone_is_kept(tmp_path):
    """同字數但同音（曉/小）仍要保留，否則會把真正的同音查詢一起殺掉。"""
    d = _with_wang_xiaoming(tmp_path)
    names = [c.person.name for c in d.search("王曉明")[0]]
    assert "王小明" in names
