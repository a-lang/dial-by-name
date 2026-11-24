# SPDX-License-Identifier: AGPL-3.0-or-later
"""`dialname/pbx_directory.py` 與 SIGHUP 重載的閘門（`docs/design.md` §名冊來源）。

閘門 1：`load_aliases()` 驗證表格 8 列全覆蓋，錯誤含全部孤兒。
閘門 2：`load_from_pbx()` 產生的 `Person` 與 JSON 版逐欄位相同。
閘門 3：alias／部門摘要有 log（含絕對路徑與 not found）。
閘門 4：`scripts/check_aliases.py` 的退出碼。
閘門 6：重載失敗保留舊索引、成功原子換掉 `(Directory, handler)`。
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

import agi_server
from dialname import pbx_directory as pbx
from dialname.core import Directory, load_directory, name_match_keys

ROOT = Path(__file__).resolve().parent.parent
DEPARTMENTS_PATH = ROOT / "data" / "departments.json"
PEOPLE_PATH = ROOT / "data" / "people.json"
SRC = "Test Directory"

WANG = pbx.PbxUser(
    id=12,
    fname="偉",
    lname="王",
    displayname="2101",
    department="業務部",
    title="經理",
    default_extension="2101",
)
DAVID = pbx.PbxUser(
    id=37,
    fname="David",
    lname="Wang",
    displayname="2102",
    department="",
    title="Engineer",
    default_extension="2102",
)


@pytest.fixture()
def departments():
    return pbx.load_departments(DEPARTMENTS_PATH)


def _write_aliases(tmp_path: Path, data: dict, name: str = "aliases.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _alias_error(tmp_path: Path, rows: list, source: str = SRC, pids=("12", "37")) -> str:
    path = _write_aliases(tmp_path, {"source_directory": source, "aliases": rows})
    with pytest.raises(ValueError) as exc:
        pbx.load_aliases(path, pids=pids, source_directory=SRC)
    return str(exc.value)


# --------------------------------------------------------------------------- #
# 部門定義
# --------------------------------------------------------------------------- #
def test_departments_file_matches_roster_departments(departments):
    """`data/departments.json` 必須與 `people.json` 的部門完全一致（單一 dept_id 契約）。"""
    assert departments == load_directory(PEOPLE_PATH).departments
    assert len(departments) == 8


def test_departments_errors_are_listed_together(tmp_path):
    path = tmp_path / "departments.json"
    path.write_text(
        json.dumps(
            [
                {"dept_id": "Sales", "name": "業務部"},
                {"dept_id": "rd", "name": ""},
                {"dept_id": "rd", "name": "工程部"},
                {"dept_id": "rd", "name": "重複ID"},
                {"dept_id": "it", "name": "資訊部", "aliases": ["MIS", "mis2"]},
                {"dept_id": "mis", "name": "網管部", "aliases": ["mis2"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        pbx.load_departments(path)
    msg = str(exc.value)
    assert "Sales" in msg                      # 非法 dept_id
    assert "name 不可為空" in msg
    assert "dept_id 重複" in msg
    assert "alias 'mis2' 同時屬於" in msg


def test_departments_missing_file_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="部門定義讀取失敗"):
        pbx.load_departments(tmp_path / "nope.json")


# --------------------------------------------------------------------------- #
# 閘門 1：aliases.json 驗證表格
# --------------------------------------------------------------------------- #
def test_alias_orphans_are_all_reported(tmp_path):
    msg = _alias_error(
        tmp_path,
        [
            {"user_id": "99", "aliases": ["小美"]},
            {"user_id": "98", "aliases": ["阿明"]},
            {"user_id": "12", "aliases": ["阿偉"]},
        ],
    )
    assert "孤兒 alias user_id='99'" in msg
    assert "孤兒 alias user_id='98'" in msg
    assert "阿偉" not in msg


def test_alias_duplicate_user_id_is_an_error(tmp_path):
    msg = _alias_error(
        tmp_path,
        [
            {"user_id": "12", "aliases": ["小美"]},
            {"user_id": "12", "aliases": ["阿美"]},
        ],
    )
    assert "user_id '12' 重複" in msg


def test_alias_empty_or_whitespace_member_is_an_error(tmp_path):
    msg = _alias_error(
        tmp_path,
        [
            {"user_id": "12", "aliases": []},
            {"user_id": "37", "aliases": ["  ", ""]},
        ],
    )
    assert "aliases 至少要 1 個非空字串" in msg
    assert "空字串或非字串" in msg


def test_alias_control_character_is_an_error(tmp_path):
    msg = _alias_error(tmp_path, [{"user_id": "12", "aliases": ["小\t美"]}])
    assert "含控制字元" in msg


def test_alias_source_directory_must_match_exactly(tmp_path):
    msg = _alias_error(tmp_path, [{"user_id": "12", "aliases": ["小美"]}], source="PBX Internal")
    assert "'PBX Internal' 與名冊 'Test Directory' 不符" in msg


def test_alias_duplicate_alias_same_person_is_deduped(tmp_path):
    path = _write_aliases(
        tmp_path, {"source_directory": SRC, "aliases": [{"user_id": "12", "aliases": ["小美", "小美"]}]}
    )
    assert pbx.load_aliases(path, pids=["12"], source_directory=SRC) == {"12": ["小美"]}


def test_same_alias_for_two_people_is_legal(tmp_path):
    path = _write_aliases(
        tmp_path,
        {
            "source_directory": SRC,
            "aliases": [
                {"user_id": "12", "aliases": ["小安"]},
                {"user_id": "37", "aliases": ["小安"]},
            ],
        },
    )
    mapping = pbx.load_aliases(path, pids=["12", "37"], source_directory=SRC)
    assert mapping == {"12": ["小安"], "37": ["小安"]}


def test_missing_alias_file_is_empty_and_logged(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="dialname.pbx"):
        assert pbx.load_aliases(tmp_path / "nope.json", pids=["12"], source_directory=SRC) == {}
    assert f"aliases: {(tmp_path / 'nope.json').resolve()} not found (0 entries)" in caplog.text


def test_alias_log_has_absolute_path_and_counts(tmp_path, caplog):
    path = _write_aliases(
        tmp_path, {"source_directory": SRC, "aliases": [{"user_id": "12", "aliases": ["小美", "阿美"]}]}
    )
    with caplog.at_level(logging.INFO, logger="dialname.pbx"):
        pbx.load_aliases(path, pids=["12"], source_directory=SRC)
    assert f"aliases: {path.resolve()} (2 entries / 1 people)" in caplog.text


# --------------------------------------------------------------------------- #
# 閘門 2：userman → Person（與 JSON 版同形）
# --------------------------------------------------------------------------- #
def test_pbx_people_equal_json_people(tmp_path, departments):
    roster = {
        "source_directory": SRC,
        "departments": [d.__dict__ for d in departments],
        "people": [
            {
                "pid": "12", "name": "王偉", "directory": SRC, "dept": "業務部",
                "title": "經理", "extension": "2101",
                "last_name": "王", "first_name": "偉", "aliases": [],
            },
            {
                "pid": "37", "name": "David Wang", "directory": SRC, "dept": "",
                "title": "Engineer", "extension": "2102",
                "last_name": "Wang", "first_name": "David", "aliases": [],
            },
        ],
    }
    path = tmp_path / "roster.json"
    path.write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    expected = load_directory(path)
    got = pbx.load_from_pbx(SRC, users=[WANG, DAVID], departments_path=DEPARTMENTS_PATH)
    assert got.people == expected.people            # 含所有衍生欄位（name_key／拼音／dept_id…）
    assert got.departments == expected.departments
    assert isinstance(got, Directory)
    assert got.person("37").name == "David Wang"
    assert got.person("37").surname_key == "wang"   # 西方名靠欄位，不靠第 1 字切姓
    assert got.person("37").dept_id == ""           # 無部門合法
    assert got.person("12").name == "王偉"           # 全 CJK → 姓前組合


def test_null_and_empty_names_are_skipped_with_warning(departments, caplog):
    users = [
        pbx.PbxUser(id=8, fname=None, lname=None, displayname="710", default_extension="710"),
        pbx.PbxUser(id=9, fname="", lname="王", displayname="711", default_extension="711"),
        pbx.PbxUser(id=10, fname=WANG.fname, lname=None, displayname="712", default_extension="712"),
        WANG,
    ]
    with caplog.at_level(logging.WARNING, logger="dialname.pbx"):
        people = pbx.build_people(SRC, users=users, departments=departments)
    assert [p.pid for p in people] == ["12"]
    for pid in ("8", "9", "10"):
        assert f"userman id={pid}" in caplog.text
    assert "fname/lname 不全" in caplog.text


def test_missing_extension_is_skipped_with_warning(departments, caplog):
    users = [pbx.PbxUser(id=20, fname="明", lname="李", displayname="2206", default_extension=None), WANG]
    with caplog.at_level(logging.WARNING, logger="dialname.pbx"):
        people = pbx.build_people(SRC, users=users, departments=departments)
    assert [p.pid for p in people] == ["12"]
    assert "userman id=20 displayname='2206': default_extension 空" in caplog.text


def test_empty_roster_is_an_error(departments):
    with pytest.raises(ValueError, match="名冊 0 人"):
        pbx.build_people(SRC, users=[], departments=departments)


def test_unresolvable_departments_are_all_reported(departments):
    users = [
        pbx.PbxUser(id=1, fname="偉", lname="王", department="不存在部", default_extension="2201"),
        pbx.PbxUser(id=2, fname="美", lname="李", department="火星部", default_extension="2202"),
    ]
    with pytest.raises(ValueError) as exc:
        pbx.build_people(SRC, users=users, departments=departments)
    msg = str(exc.value)
    assert "person '1'" in msg and "不存在部" in msg
    assert "person '2'" in msg and "火星部" in msg


def test_aliases_are_merged_into_alias_keys(tmp_path, departments):
    aliases = _write_aliases(
        tmp_path, {"source_directory": SRC, "aliases": [{"user_id": "12", "aliases": ["小美"]}]}
    )
    directory = pbx.load_from_pbx(
        SRC, users=[WANG], departments_path=DEPARTMENTS_PATH, aliases_path=aliases
    )
    person = directory.person("12")
    assert person.aliases == ["小美"]
    assert person.alias_keys == name_match_keys("小美")


def test_load_from_pbx_needs_users_or_db():
    with pytest.raises(ValueError, match="需要 users 或 db"):
        pbx.load_from_pbx(SRC, departments_path=DEPARTMENTS_PATH)


# --------------------------------------------------------------------------- #
# DB 連線來源：整組二擇一
# --------------------------------------------------------------------------- #
def _conf(tmp_path: Path, **values: str) -> Path:
    lines = ["<?php", "// $amp_conf['AMPDBHOST'] = 'commented-out';", "$foo = 1;"]
    for key, value in values.items():
        lines.append(f"$amp_conf['{key}'] = '{value}';")
    path = tmp_path / "freepbx.conf"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_env_wins_and_conf_is_not_read(tmp_path):
    conf = _conf(tmp_path, AMPDBHOST="confhost", AMPDBUSER="confuser", AMPDBPASS="x", AMPDBNAME="asterisk")
    cfg = pbx.resolve_db_config(
        {"DBN_MYSQL_HOST": "envhost", "DBN_MYSQL_USER": "envuser", "DBN_MYSQL_PASS": "envpass", "DBN_MYSQL_DB": "envdb"},
        conf_path=conf,
    )
    assert (cfg.host, cfg.user, cfg.password, cfg.database) == ("envhost", "envuser", "envpass", "envdb")


def test_partial_env_falls_back_to_whole_conf(tmp_path):
    conf = _conf(tmp_path, AMPDBHOST="localhost", AMPDBUSER="asteriskuser", AMPDBPASS="p", AMPDBNAME="asterisk")
    cfg = pbx.resolve_db_config({"DBN_MYSQL_HOST": "envhost"}, conf_path=conf)
    # 不混搭：host 也來自 conf（註解掉的那行不算）
    assert (cfg.host, cfg.user, cfg.password, cfg.database) == ("localhost", "asteriskuser", "p", "asterisk")


def test_incomplete_sources_refuse_startup(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        pbx.resolve_db_config({"DBN_MYSQL_USER": "u"}, conf_path=tmp_path / "missing.conf")
    msg = str(exc.value)
    assert "DBN_MYSQL_HOST" in msg and "DBN_MYSQL_PASS" in msg and "DBN_MYSQL_DB" in msg
    assert "AMPDBHOST" in msg and "AMPDBNAME" in msg


def test_db_config_str_hides_password():
    cfg = pbx.DbConfig(host="h", user="u", password="secret", database="d")
    assert "secret" not in str(cfg)


# --------------------------------------------------------------------------- #
# 旗標契約
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["--pbx-db"],
        ["--source-directory", "Test Directory"],
        ["--aliases", "x.json"],
        ["--departments", "x.json"],
    ],
)
def test_agi_server_rejects_inconsistent_flags(argv):
    with pytest.raises(SystemExit):
        agi_server.main(argv)


# --------------------------------------------------------------------------- #
# 閘門 6：SIGHUP 重載
# --------------------------------------------------------------------------- #
def _state(build):
    current = agi_server.make_current(load_directory(PEOPLE_PATH), stt=object(), log_dir="out/calls")
    return agi_server.State(current=current, log_dir="out/calls", build=build)


def test_reload_failure_keeps_previous_index(caplog):
    def failing() -> agi_server.Current:
        raise ValueError("aliases 驗證失敗：\n  - 孤兒 alias user_id='37'")

    state = _state(failing)
    old = state.current
    with caplog.at_level(logging.ERROR, logger="dialname.agi"):
        assert agi_server.apply_reload(state) is False
    assert state.current is old
    assert "reload failed:" in caplog.text
    assert "孤兒 alias user_id='37'" in caplog.text
    assert "keeping previous index" in caplog.text


def test_reload_success_swaps_directory_and_handler(tmp_path, caplog):
    new_dir = pbx.load_from_pbx(
        SRC,
        users=[WANG],
        departments_path=DEPARTMENTS_PATH,
        aliases_path=_write_aliases(
            tmp_path, {"source_directory": SRC, "aliases": [{"user_id": "12", "aliases": ["小美"]}]}
        ),
    )
    state = _state(lambda: agi_server.make_current(new_dir, stt=object(), log_dir="out/calls"))
    old = state.current
    with caplog.at_level(logging.INFO, logger="dialname.agi"):
        assert agi_server.apply_reload(state) is True
    assert state.current is not old
    assert state.current.directory is new_dir
    # handle_turn 必須綁新索引，不能只換 state.directory
    assert state.current.handler.keywords["directory"] is new_dir
    was = sum(len(p.aliases) for p in old.directory.people)
    assert f"reload ok: people=1 aliases=1 (was {was})" in caplog.text


# --------------------------------------------------------------------------- #
# 閘門 4：scripts/check_aliases.py
# --------------------------------------------------------------------------- #
def _load_check_script():
    spec = importlib.util.spec_from_file_location("check_aliases", ROOT / "scripts" / "check_aliases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_check(monkeypatch, module, departments, users):
    monkeypatch.setattr(module, "resolve_db_config", lambda: object())
    monkeypatch.setattr(module, "fetch_users", lambda db, source: users)
    monkeypatch.setattr(module, "load_departments", lambda path: departments)


def test_check_aliases_exit_code(tmp_path, monkeypatch, capsys, departments):
    module = _load_check_script()
    _patch_check(monkeypatch, module, departments, [WANG])
    bad = _write_aliases(tmp_path, {"source_directory": SRC, "aliases": [{"user_id": "99", "aliases": ["小美"]}]})

    assert module.main(["--source-directory", SRC, "--aliases", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "孤兒 alias user_id='99'" in err

    good = _write_aliases(tmp_path, {"source_directory": SRC, "aliases": [{"user_id": "12", "aliases": ["小美"]}]})
    assert module.main(["--source-directory", SRC, "--aliases", str(good)]) == 0
    assert "OK: source='Test Directory' people=1 aliases=1 people_with_alias=1" in capsys.readouterr().out
