# SPDX-License-Identifier: AGPL-3.0-or-later
"""dialname.pbx_directory — 正式名冊入口：FreePBX userman ＋ `aliases.json` sidecar。

契約（與 `core.load_directory` 相同）：產生的 `Person` / `Department` / `Directory` 形狀
完全一致，下游（`name_match_keys()`、`alias-ask`、`alias-not-found`）不知道資料從哪來。
所以切換與回滾都只是換入口（`docs/design.md` §名冊來源）。

資料來源：
  userman_users  JOIN userman_directories（`auth` 是整數 FK）
  aliases.json   sidecar，稀疏：只登記需要小名的人；key 是 `userman_users.id`，不是分機。
  departments    `data/departments.json`（userman 只有人的自由文字 department，沒有部門清單）

驗證一律「收集全部問題再一次報錯」：IT 改檔時不用一輪一輪修。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterable, Mapping

import pymysql

from .core import (
    CJK_RE,
    DEPT_ID_RE,
    Department,
    Directory,
    Person,
    _resolve_department,
    build_person,
    normalize,
)

log = logging.getLogger("dialname.pbx")

DEFAULT_DEPARTMENTS_PATH = "data/departments.json"
DEFAULT_ALIASES_PATH = "data/aliases.json"
FREEPBX_CONF = Path("/etc/freepbx.conf")

USERS_SQL = """
SELECT u.id, u.fname, u.lname, u.displayname, u.department, u.title, u.default_extension
FROM userman_users u
JOIN userman_directories d ON u.auth = d.id
WHERE d.name = %(source_directory)s
"""

# 連線來源整組二擇一（不混搭）：`.env` 四鍵齊備優先，任一缺才 fallback freepbx.conf。
DB_FIELDS = ("host", "user", "pass", "db")
ENV_KEYS = {
    "host": "DBN_MYSQL_HOST",
    "user": "DBN_MYSQL_USER",
    "pass": "DBN_MYSQL_PASS",
    "db": "DBN_MYSQL_DB",
}
AMP_KEYS = {
    "host": "AMPDBHOST",
    "user": "AMPDBUSER",
    "pass": "AMPDBPASS",
    "db": "AMPDBNAME",
}
# 嚴格最小：只認 `$amp_conf['KEY'] = 'value';`。其餘 PHP（含被註解掉的賦值）一律忽略。
_AMP_LINE_RE = re.compile(
    r"""^\s*\$amp_conf\s*\[\s*(['"])(?P<key>[A-Za-z_][A-Za-z0-9_]*)\1\s*\]\s*=\s*(['"])(?P<value>.*?)\3\s*;"""
)


@dataclass(frozen=True)
class DbConfig:
    host: str
    user: str
    password: str
    database: str

    def __str__(self) -> str:
        return f"{self.user}@{self.host}/{self.database}"  # 密碼絕不進 log


@dataclass(frozen=True)
class PbxUser:
    """`USERS_SQL` 的一列。SQL NULL 會是 None，由 `build_people()` 統一視同空字串。"""

    id: int
    fname: str | None = None
    lname: str | None = None
    displayname: str | None = None
    department: str | None = None
    title: str | None = None
    default_extension: str | None = None


# --------------------------------------------------------------------------- #
# 部門定義（`data/departments.json`）
# --------------------------------------------------------------------------- #
def load_departments(path: str | Path) -> list[Department]:
    """載入 `Department[]`；規則與 `core.load_directory` 的 JSON 版相同（收集後一次報錯）。"""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"部門定義讀取失敗 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"部門定義不是合法 JSON {path}：{exc}") from exc
    if not isinstance(rows, list):
        raise ValueError(f"部門定義 {path}：頂層必須是部門陣列")

    errors: list[str] = []
    departments: list[Department] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for i, row in enumerate(rows):
        where = f"department #{i}"
        if not isinstance(row, dict):
            errors.append(f"{where}: 必須是物件")
            continue
        dept_id = row.get("dept_id")
        name = row.get("name")
        aliases = row.get("aliases", [])
        if not isinstance(dept_id, str) or not DEPT_ID_RE.fullmatch(dept_id):
            errors.append(f"{where}: dept_id {dept_id!r} 不合 ^[a-z][a-z0-9_]*$")
            continue
        if not isinstance(name, str) or not normalize(name):
            errors.append(f"{where} ({dept_id}): name 不可為空")
            continue
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            errors.append(f"{where} ({dept_id}): aliases 必須是字串陣列")
            continue
        norm = normalize(name)
        if dept_id in seen_ids:
            errors.append(f"部門 dept_id 重複：{dept_id!r}")
            continue
        if norm in seen_names:
            errors.append(f"部門 name 重複：{norm!r}")
            continue
        seen_ids.add(dept_id)
        seen_names.add(norm)
        departments.append(Department(dept_id=dept_id, name=norm, aliases=list(aliases)))

    # 同一講法只能屬於一個部門（與 JSON 版同規則）
    alias_owner: dict[str, str] = {}
    for d in departments:
        for alias in (d.name, *d.aliases):
            key = normalize(alias)
            if not key:
                continue
            prev = alias_owner.get(key)
            if prev is not None and prev != d.dept_id:
                errors.append(f"部門 alias {alias!r} 同時屬於 {prev!r} 與 {d.dept_id!r}")
            alias_owner[key] = d.dept_id
    if errors:
        raise ValueError(_format_errors("部門定義驗證失敗", errors))
    return departments


# --------------------------------------------------------------------------- #
# userman → Person[]
# --------------------------------------------------------------------------- #
def _text(value: object) -> str:
    """SQL NULL／空白字串一律視同空（正式冊實測兩種並存）。"""
    return str(value).strip() if value is not None else ""


def _compose_name(fname: str, lname: str) -> str:
    """全 CJK → 姓前（`lname + fname`）；否則西方順序（`fname + " " + lname`）。

    姓氏／名字的特徵走 `last_name` / `first_name` 欄位，比對不靠串接順序；
    這裡只決定顯示名（payload／log）。
    """
    joined = lname + fname
    if all(CJK_RE.fullmatch(ch) for ch in joined):
        return joined
    return f"{fname} {lname}"


def build_people(
    source_directory: str, *, users: Iterable[PbxUser], departments: list[Department]
) -> list[Person]:
    """userman 列 → `Person[]`（已 `build_person()`，但沒有 alias）。

    跳過（＋WARNING，不報錯）：
      - `fname` 或 `lname` 任一缺：displayname 不作名稱來源（本站慣例＝分機號，且西方名會切錯姓）
      - `default_extension` 空：管理／測試帳號，用 JSON 版的「報錯」會讓正式環境起不來
    報錯（收集全部後一次拋）：
      - 部門非空但對不到 `name`／`aliases`
      - `pid` 重複
    """
    errors: list[str] = []
    people: list[Person] = []
    seen_pids: set[str] = set()
    for u in users:
        pid = str(u.id)
        label = f"userman id={pid} displayname={_text(u.displayname)!r}"
        fname, lname = _text(u.fname), _text(u.lname)
        extension = _text(u.default_extension)
        if not fname or not lname:
            log.warning("%s: fname/lname 不全，跳過", label)
            continue
        if not extension:
            log.warning("%s: default_extension 空，跳過", label)
            continue
        if pid in seen_pids:
            errors.append(f"person pid 重複：{pid!r}（{label}）")
            continue
        seen_pids.add(pid)
        p = Person(
            pid=pid,
            name=_compose_name(fname, lname),
            directory=source_directory,
            dept=_text(u.department),
            title=_text(u.title),
            extension=extension,
            last_name=lname,
            first_name=fname,
        )
        try:
            p.dept_id, p.dept_name = _resolve_department(p.dept, departments, p.pid)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        people.append(p)
    if errors:
        raise ValueError(_format_errors("名冊驗證失敗", errors))
    if not people:
        raise ValueError(
            f"名冊 0 人（source_directory={source_directory!r}）："
            "拒絕以空名冊啟動——directory 被 rename／權限不對時每一通都會靜默轉總機"
        )
    for p in people:
        build_person(p)
    return people


# --------------------------------------------------------------------------- #
# aliases.json
# --------------------------------------------------------------------------- #
def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def load_aliases(
    path: str | Path, *, pids: Collection[str], source_directory: str
) -> dict[str, list[str]]:
    """載入並驗證 sidecar；回傳 `pid → aliases`（同人重複去重）。

    檔案不存在＝空（合法）：功能關閉與「沒人需要小名」無法從筆數分辨，
    所以 log 一定要印絕對路徑。驗證錯誤一次全列。
    """
    resolved = Path(path).resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("aliases: %s not found (0 entries)", resolved)
        return {}
    except OSError as exc:
        raise ValueError(f"aliases 讀取失敗 {resolved}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"aliases 不是合法 JSON {resolved}：{exc}") from exc

    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValueError(f"aliases {resolved}：頂層必須是物件")
    source = data.get("source_directory")
    if not isinstance(source, str) or not source:
        errors.append("source_directory 必填（必須逐字等於名冊的 userman_directories.name）")
    elif source != source_directory:
        errors.append(f"source_directory {source!r} 與名冊 {source_directory!r} 不符")
    rows = data.get("aliases")
    if not isinstance(rows, list):
        raise ValueError(_format_errors("aliases 驗證失敗", errors + ["aliases 必須是陣列"]))

    known = set(pids)
    mapping: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    for i, row in enumerate(rows):
        where = f"aliases[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: 必須是 {{user_id, aliases}} 物件")
            continue
        user_id = row.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            errors.append(f"{where}: user_id 必填（userman_users.id 的字串）")
            continue
        if user_id in seen_ids:
            errors.append(f"{where}: user_id {user_id!r} 重複")
            continue
        seen_ids.add(user_id)
        raw = row.get("aliases")
        if not isinstance(raw, list) or not raw:
            errors.append(f"{where} ({user_id}): aliases 至少要 1 個非空字串")
            continue
        aliases: list[str] = []
        for alias in raw:
            if not isinstance(alias, str) or not alias.strip():
                errors.append(f"{where} ({user_id}): alias {alias!r} 是空字串或非字串")
                continue
            if _has_control(alias):
                errors.append(f"{where} ({user_id}): alias {alias!r} 含控制字元")
                continue
            if alias not in aliases:
                aliases.append(alias)
        if not aliases:
            continue
        if user_id not in known:
            errors.append(f"{where}: 孤兒 alias user_id={user_id!r}（名冊找不到，離職請一併刪除）")
            continue
        mapping[user_id] = aliases
    if errors:
        raise ValueError(_format_errors("aliases 驗證失敗", errors))

    entries = sum(len(v) for v in mapping.values())
    log.info("aliases: %s (%d entries / %d people)", resolved, entries, len(mapping))
    return mapping


# --------------------------------------------------------------------------- #
# DB 連線
# --------------------------------------------------------------------------- #
def _read_amp_conf(path: Path) -> tuple[dict[str, str], str]:
    """回傳 (四鍵值, 問題描述)。只認 `$amp_conf[...] = '...';` 賦值行。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, f"{path} 不可讀（{exc.strerror or exc}）"
    found: dict[str, str] = {}
    for line in text.splitlines():
        m = _AMP_LINE_RE.match(line)
        if m and m.group("key") in AMP_KEYS.values():
            found[m.group("key")] = m.group("value")   # PHP 語意：後面的賦值覆蓋前面的
    return found, ""


def resolve_db_config(
    env: Mapping[str, str] | None = None, *, conf_path: Path = FREEPBX_CONF
) -> DbConfig:
    """`.env` 四鍵齊備優先；任一缺 → fallback freepbx.conf；都不齊 → 拒絕啟動。

    整組二擇一、不混搭：混搭會出現「host 來自 .env、帳號來自 root 的 freepbx.conf」
    這種沒人預期的權限組合。
    """
    env = os.environ if env is None else env
    values = {f: (env.get(ENV_KEYS[f]) or "").strip() for f in DB_FIELDS}
    missing_env = [ENV_KEYS[f] for f in DB_FIELDS if not values[f]]
    if not missing_env:
        return DbConfig(
            host=values["host"], user=values["user"], password=values["pass"], database=values["db"]
        )

    amp, problem = _read_amp_conf(conf_path)
    if not problem:
        missing_amp = [AMP_KEYS[f] for f in DB_FIELDS if not (amp.get(AMP_KEYS[f]) or "").strip()]
        if not missing_amp:
            return DbConfig(
                host=amp[AMP_KEYS["host"]].strip(),
                user=amp[AMP_KEYS["user"]].strip(),
                password=amp[AMP_KEYS["pass"]],
                database=amp[AMP_KEYS["db"]].strip(),
            )
    else:
        missing_amp = [AMP_KEYS[f] for f in DB_FIELDS]
    raise RuntimeError(
        "資料庫設定不完整，拒絕啟動："
        f".env 缺 {', '.join(missing_env)}；"
        f"{conf_path} 缺 {', '.join(missing_amp)}" + (f"（{problem}）" if problem else "")
    )


def fetch_users(db: DbConfig, source_directory: str) -> list[PbxUser]:
    """查 `userman_users`（join directory）。連線失敗＝啟動失敗，不以空名冊繼續。"""
    try:
        conn = pymysql.connect(
            host=db.host,
            user=db.user,
            password=db.password,
            database=db.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=10,
        )
    except pymysql.MySQLError as exc:
        raise RuntimeError(f"連線 PBX 資料庫失敗（{db}）：{exc}") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_SQL, {"source_directory": source_directory})
            rows = cur.fetchall()
    except pymysql.MySQLError as exc:
        raise RuntimeError(f"查詢 userman_users 失敗（{db}）：{exc}") from exc
    finally:
        conn.close()
    return [PbxUser(**row) for row in rows]


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def load_from_pbx(
    source_directory: str,
    *,
    departments_path: str | Path = DEFAULT_DEPARTMENTS_PATH,
    aliases_path: str | Path | None = DEFAULT_ALIASES_PATH,
    db: DbConfig | None = None,
    users: Iterable[PbxUser] | None = None,
) -> Directory:
    """組出與 `core.load_directory()` 形狀相同的 `Directory`。

    `users` 省略時用 `db` 查線上的 userman；測試傳固定列就不碰 DB。
    """
    departments = load_departments(departments_path)
    if users is None:
        if db is None:
            raise ValueError("load_from_pbx 需要 users 或 db 其中一個")
        users = fetch_users(db, source_directory)
    people = build_people(source_directory, users=users, departments=departments)
    aliases = (
        load_aliases(aliases_path, pids=[p.pid for p in people], source_directory=source_directory)
        if aliases_path is not None
        else {}
    )
    for p in people:
        p.aliases = aliases.get(p.pid, [])
        # 重算一次讓 `alias_keys` 含小名（`Directory` 的 alias 分支靠它）
        build_person(p)
    directory = Directory(people, departments)
    log.info(
        "roster: source=%r people=%d aliases=%d departments=%d",
        source_directory,
        len(people),
        sum(len(p.aliases) for p in people),
        len(departments),
    )
    return directory


def _format_errors(title: str, errors: list[str]) -> str:
    return title + "：\n" + "\n".join(f"  - {e}" for e in errors)
