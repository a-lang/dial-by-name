# SPDX-License-Identifier: AGPL-3.0-or-later
"""dialname.agi — AGI 的單輪處理器（STT → decide → DBN_*）。

這個模組**不碰電話層**：不播音、不轉接、不管回合、不讀上一輪狀態。
dialplan 傳入 `wav + attempt + DBN_CANDS`，這裡只回六個通道變數：

    DBN_ACTION / DBN_TARGET / DBN_DEPT / DBN_SPEECH / DBN_REASON / DBN_CANDS

`DBN_CANDS`（`pid:score&…`）是給**下一輪**用的：第二輪只講部門時，
用它把上一輪的候選收斂到該部門。狀態存在通道變數上，AGI 自己不保存任何東西。

失敗對應（與 docs/dialplan.md 一致）：
  空逐字稿／STT timeout／5xx → retry（由 dialplan 的 DBN_TRIES 決定何時掛線）
  401/403                   → hangup（key 有問題時不灌爆總機）
  未預期例外                → fallback（dialplan 轉總機）
"""

from __future__ import annotations

import json
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .core import Candidate, Directory
from .decision import Decision, decide
from .stt_client import SttAuthError, SttError

CLIP_PREFIX = "sounds/zh/"
GOODBYE_CLIP = "goodbye"

# 跨輪沿用候選（`DBN_CANDS`）的序列化格式：`pid:score&pid:score`。
# 用 `&` 當分隔是因為 AGI 的參數是逗號分隔的，逗號會把一個變數拆成多個參數。
CARRY_SEP = "&"
CARRY_MAX = 5


class SttLike(Protocol):
    def transcribe(self, path):  # -> Transcript
        ...


def _parse_carry(raw: str | None) -> list[tuple[str, float]]:
    """解析 `DBN_CANDS`；格式不合的整筆跳過（通道變數可能來自舊版 dialplan）。"""
    pairs: list[tuple[str, float]] = []
    for item in (raw or "").split(CARRY_SEP):
        pid, _, score = item.strip().partition(":")
        if not pid or not pid.isascii() or not all(c.isalnum() or c == "_" for c in pid):
            continue
        try:
            value = float(score)
        except ValueError:
            continue
        pairs.append((pid, value))
    return pairs[:CARRY_MAX]


def _format_carry(pairs: list[tuple[str, float]]) -> str:
    return CARRY_SEP.join(f"{pid}:{score:.3f}" for pid, score in pairs[:CARRY_MAX])


def _carry_from_prev(prev: list[tuple[str, float]], directory: Directory,
                     dept) -> list[Candidate] | None:
    """第二輪只講部門時，用上一輪的候選收斂到該部門；沒有交集回 None。"""
    narrowed = []
    for pid, score in prev:
        person = directory.person(pid)
        if person is not None and person.dept_id == dept.dept_id:
            narrowed.append(Candidate(person, score))
    return narrowed or None


def _clip_name(clip: str) -> str:
    return clip[len(CLIP_PREFIX):] if clip.startswith(CLIP_PREFIX) else clip


def _to_result(dec: Decision, cands: str = "") -> dict:
    target = dec.target or ""
    return {
        "action": dec.action,
        "target": target,
        "dept": target if dec.action == "route_dept" else "",
        "speech": [_clip_name(s) for s in dec.speech],
        "reason": dec.reason,
        "cands": cands,
    }


def _ascii_result(result: dict, reason: str = "non-ascii") -> dict:
    """通道變數只接受安全的 ASCII：不含控制字元、引號、反斜線。

    只檢查 `isascii()` 不夠——`\\n`／`\\r`／`"` 都是 ASCII，但會讓 AGI 指令注入或 desync。
    """
    def safe(value) -> bool:
        if not isinstance(value, str) or not value.isascii():
            return False
        return all(32 <= ord(c) < 127 and c not in '"\\' for c in value)

    values = [result.get(k, "") for k in ("action", "target", "dept", "reason", "cands")]
    values += list(result.get("speech") or [])
    if all(safe(v) for v in values):
        return result
    return {"action": "fallback", "target": "", "dept": "", "speech": [],
            "reason": reason, "cands": ""}


def _wav_duration_ms(path: str | Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)
    except Exception:  # noqa: BLE001 — 量不到不是致命錯誤
        return None


def _is_network_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return any(t in name for t in ("timeout", "connect", "network", "readerror"))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_log(log_dir: str | Path | None, record: dict) -> None:
    if log_dir is None:
        return
    path = Path(log_dir) / f"{datetime.now():%Y%m%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:      # O_APPEND，一行一輪
        f.write(line + "\n")


def handle_turn(
    wav: str | Path,
    attempt: str | int,
    *,
    stt: SttLike,
    directory: Directory,
    disclose_names: bool = False,
    log_dir: str | Path | None = None,
    call_id: str | None = None,
    prev_pids: str | None = None,
) -> dict:
    """回傳 DBN_* 對應的 dict；不碰 channel、不播音、不轉接。

    `prev_pids` 是上一輪的 `DBN_CANDS`（`pid:score&…`）。第二輪如果只講部門，
    就靠它把上一輪的候選收斂到那個部門——AGI 本身仍然不讀任何跨輪狀態檔。
    """
    prev = _parse_carry(prev_pids)
    prev_echo = _format_carry(prev)
    carried = False
    dept_ignored = None
    attempt_raw = attempt
    try:
        attempt_int = int(attempt)
        if attempt_int < 1:
            attempt_int = 1
    except (TypeError, ValueError):
        attempt_int = 1

    duration_ms = _wav_duration_ms(wav)
    stt_ms: int | None = None
    text = ""
    lang = None
    cands = []
    stt_status = "ok"
    decision = None

    started = time.monotonic()
    try:
        t0 = time.monotonic()
        tr = stt.transcribe(wav)
        stt_ms = int((time.monotonic() - t0) * 1000)
        text = (tr.text or "").strip()
        lang = tr.language
        if not text:
            stt_status = "empty"
            result = {"action": "retry", "target": "", "dept": "",
                      "speech": [], "reason": "stt-empty"}
        else:
            cands, dept, q, meta = directory.search_meta(text)
            # 第二輪只講部門（「客服部」）時，用上一輪候選收斂到該部門；
            # 沒有這一步，第一輪已經算出的正確人選會在第二輪被丟掉。
            if not cands and dept is not None:
                if prev:
                    narrowed = _carry_from_prev(prev, directory, dept)
                    if narrowed:
                        cands = narrowed
                        carried = True
                    else:
                        # 來電者已經講過人名：這句部門是澄清答案，不是「要轉部門」的指令。
                        # 收斂不到人就交總機，不要默默把「找陳美玲」變成「轉人事部」。
                        dept_ignored = dept.dept_id
                        dept = None
            decision = decide(
                [text], cands, dept,
                attempt=attempt_int,
                departments=directory.departments,
                dept_residual=q,            # 必傳，否則「只講部門」會被誤掛線
                disclose_names=disclose_names,
                surnames=directory.surnames,
                # 只講名／小名的分支結果：alias 命中 or 像某人的名卻未登記
                name_form="alias" if meta.get("alias") else (
                    "given" if meta.get("given_like") else None),
            )
            result = _to_result(decision, _format_carry(
                [(c.person.pid, c.score) for c in cands[:CARRY_MAX]]))
    except SttAuthError as exc:
        stt_status = str(getattr(exc, "status_code", 401))
        result = {"action": "hangup", "target": "", "dept": "",
                  "speech": [GOODBYE_CLIP], "reason": "stt-auth"}
    except (SttError, OSError, TimeoutError) as exc:
        code = getattr(exc, "status_code", None)
        stt_status = str(code) if code else (
            "timeout" if _is_network_error(exc) else type(exc).__name__
        )
        result = {"action": "retry", "target": "", "dept": "",
                  "speech": [], "reason": "stt-error"}
    except Exception as exc:  # noqa: BLE001 — 任何未預期錯誤都不讓來電者卡住
        if _is_network_error(exc):
            stt_status = type(exc).__name__
            result = {"action": "retry", "target": "", "dept": "",
                      "speech": [], "reason": "stt-error"}
        else:
            stt_status = "exception"
            result = {"action": "fallback", "target": "", "dept": "",
                      "speech": [], "reason": "internal-error"}

    # retry／例外路徑沒有候選：把上一輪的帶回去，下一輪（retry 後）還用得到。
    result.setdefault("cands", prev_echo)
    result = _ascii_result(result)
    decide_ms = int((time.monotonic() - started) * 1000) - (stt_ms or 0)

    record = {
        "ts": _now(),
        "call_id": call_id,
        "attempt": attempt_int,
        "wav": str(wav),
        "wav_duration_ms": duration_ms,
        "stt": {"text": text, "lang": lang, "ms": stt_ms},
        "candidates": [{"pid": c.person.pid, "score": c.score} for c in cands[:5]],
        "decision": {
            "action": result["action"],
            "target": result["target"] or None,
            "reason": result["reason"],
            "confidence": getattr(decision, "confidence", None),
            "speech": result["speech"],
        },
        "stt_status": stt_status,
        "decide_ms": max(decide_ms, 0),
    }
    if str(attempt_raw) != str(attempt_int):
        record["attempt_raw"] = attempt_raw
    if carried:
        # 這一輪的候選不是重新比對來的，是上一輪候選被部門過濾出來的
        record["carried_from_prev"] = True
    if dept_ignored:
        # 這句部門被當成澄清答案（不是轉部門指令），而且和上一輪候選沒有交集
        record["dept_ignored"] = dept_ignored
    _append_log(log_dir, record)
    return result


# --------------------------------------------------------------------------- #
# AGI 薄協議層（行導向文字）
# --------------------------------------------------------------------------- #
class AgiProtocolError(RuntimeError):
    pass


class AgiSession:
    """FastAGI 的行導向協議：初始 env → SET VARIABLE → 關閉連線。"""

    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self.env: dict[str, str] = {}

    def _readline(self) -> str | None:
        raw = self.rfile.readline()
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return raw.rstrip("\r\n")

    def _write(self, line: str) -> None:
        data = line.encode("utf-8") + b"\n"
        self.wfile.write(data)
        self.wfile.flush()

    def _command(self, line: str) -> None:
        self._write(line)
        resp = self._readline()
        if resp is None:
            raise AgiProtocolError("Asterisk 關閉連線")
        if resp.startswith("200"):
            return
        if resp.startswith("520-"):
            # Asterisk 的 usage 回應可能是多行（520-... / usage / 520 End of proper usage.）；
            # 一定要讀完，否則下一個指令會讀到殘留的 usage 行。
            while True:
                more = self._readline()
                if more is None or more.startswith("520 End of proper usage"):
                    break
            raise AgiProtocolError(f"AGI 指令語法錯誤：{line!r}")
        if resp.startswith("520"):
            # 沒有註冊 usage 的指令是單行 520（不會有 End 行）；直接拋，不要等 socket timeout。
            raise AgiProtocolError(f"AGI 指令語法錯誤：{line!r}")
        raise AgiProtocolError(f"AGI 指令失敗：{line} → {resp}")

    def read_env(self) -> dict[str, str]:
        while True:
            line = self._readline()
            if line is None:
                raise AgiProtocolError("初始環境不完整")
            if line == "":
                break
            if ":" in line:
                key, value = line.split(":", 1)
                self.env[key.strip()] = value.strip()
        return self.env

    def set_variable(self, name: str, value: str) -> None:
        # Asterisk handle_setvariable() 要求 argc == 4；空值若不給引號會變成 argc == 3
        # → 520 語法錯誤。一律加引號（值已在 _ascii_result 驗過不含 " 或 \）。
        if any(ord(c) < 32 or ord(c) == 127 or c in '"\\' for c in value):
            raise AgiProtocolError(f"通道變數含不安全字元：{name}")
        self._command(f'SET VARIABLE {name} "{value}"')


def run_session(session: AgiSession, *, handler) -> dict:
    """讀初始 env、呼叫 handler、寫回五個變數；handler 的簽名見 handle_turn。"""
    env = session.read_env()
    wav = env.get("agi_arg_1", "")
    attempt = env.get("agi_arg_2", "1")
    prev_pids = env.get("agi_arg_3", "")
    if not wav:
        raise AgiProtocolError("缺少 agi_arg_1（wav）")
    result = handler(wav, attempt, prev_pids=prev_pids, call_id=env.get("agi_uniqueid"))
    for key, var in (
        ("action", "DBN_ACTION"),
        ("target", "DBN_TARGET"),
        ("dept", "DBN_DEPT"),
        ("speech", "DBN_SPEECH"),
        ("reason", "DBN_REASON"),
        ("cands", "DBN_CANDS"),
    ):
        value = result.get(key, "")
        if isinstance(value, (list, tuple)):
            value = "&".join(value)
        session.set_variable(var, str(value))
    return result

