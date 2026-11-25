# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAGI 的營運狀態：sweeper heartbeat 與 JSONL 新鮮度。

sweeper 是 liveness 的一部分（`.plan/plan-realtime-stt.md` §3.4／§6）：它死掉時
主行程與 port 都還在，但過期 session 會永佔並發閘。所以 sweeper 每一輪把
heartbeat 寫成檔案，`scripts/agi.sh status` 以「檔案的 pid 相符＋時間夠新」判定
存活——不需要 metrics 系統，也不需要額外 port。

heartbeat 用 `os.replace` 原子換檔，`status` 不會讀到半寫入的 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SWEEP_INTERVAL = 1.0          # sweeper 週期（秒）；TTL 是 20s，1s 粒度足夠
HEARTBEAT_MAX_AGE = 5.0       # 超過這麼久沒更新＝sweeper 死了（允許幾次排程抖動）

DEFAULT_HEALTH_PATH = "out/agi.health.json"
DEFAULT_LOG_DIR = "out/calls"


@dataclass(frozen=True)
class SweeperHealth:
    alive: bool
    reason: str                 # ok／missing／stale／pid-mismatch／unreadable
    age: float | None
    active: int | None
    last_sweep: str | None


def write_heartbeat(
    path: str | Path,
    *,
    pid: int,
    active: int,
    interval: float = SWEEP_INTERVAL,
    now: float | None = None,
) -> None:
    """寫一筆 heartbeat；`now` 是 epoch 秒（測試用，預設 wall clock）。"""
    epoch = time.time() if now is None else now
    record = {
        "pid": pid,
        "interval": interval,
        "active_sessions": active,
        "last_sweep": datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds"),
        "last_sweep_epoch": epoch,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def _max_age(interval: float | None, *, default: float) -> float:
    # 週期拉長時（測試會用 0.01–0.02s）門檻跟著放寬；預設仍以 HEARTBEAT_MAX_AGE 為準
    return max(default, 3.0 * float(interval or SWEEP_INTERVAL))


def read_health(
    path: str | Path,
    *,
    pid: int | None = None,
    now: float | None = None,
    max_age: float = HEARTBEAT_MAX_AGE,
) -> SweeperHealth:
    """讀 heartbeat。`pid` 有給就同時驗「這檔是不是這個行程寫的」。"""
    target = Path(path)
    if not target.exists():
        return SweeperHealth(False, "missing", None, None, None)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
        file_pid = record.get("pid")
        epoch = float(record["last_sweep_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return SweeperHealth(False, "unreadable", None, None, None)

    if pid is not None and file_pid != pid:
        return SweeperHealth(False, "pid-mismatch", None, record.get("active_sessions"),
                             record.get("last_sweep"))

    age = max(0.0, (time.time() if now is None else now) - epoch)
    alive = age <= _max_age(record.get("interval"), default=max_age)
    return SweeperHealth(
        alive,
        "ok" if alive else "stale",
        age,
        record.get("active_sessions"),
        record.get("last_sweep"),
    )


def latest_jsonl(log_dir: str | Path) -> tuple[Path, str] | None:
    """回最新 JSONL 檔與最後一筆 `ts`；沒有檔／讀不到就回 None。"""
    directory = Path(log_dir)
    try:
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 16384))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("ts"):
                return path, str(record["ts"])
    return None


def describe_health(status: SweeperHealth, path: str | Path) -> str:
    if status.reason == "ok":
        return (f"sweeper: alive  age={status.age:.1f}s  active={status.active}"
                f"  last_sweep={status.last_sweep}")
    if status.reason == "missing":
        return f"sweeper: DEAD（找不到 heartbeat：{path}）"
    if status.reason == "stale":
        return (f"sweeper: DEAD（heartbeat {status.age:.1f}s 前，超過 {HEARTBEAT_MAX_AGE:.0f}s；"
                f"active={status.active}）")
    if status.reason == "pid-mismatch":
        return f"sweeper: DEAD（heartbeat 屬於別的行程：{path}）"
    return f"sweeper: DEAD（heartbeat 無法解析：{path}）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FastAGI sweeper／JSONL 狀態")
    ap.add_argument("--health", default=DEFAULT_HEALTH_PATH)
    ap.add_argument("--pid", type=int, help="服務 pid；給了就判定 sweeper 存活（死了回非零）")
    ap.add_argument("--logs", default=DEFAULT_LOG_DIR)
    args = ap.parse_args(argv)

    code = 0
    if args.pid is not None:
        status = read_health(args.health, pid=args.pid)
        print(describe_health(status, args.health))
        if not status.alive:
            code = 1
    latest = latest_jsonl(args.logs)
    if latest is None:
        print(f"jsonl: none（{args.logs}）")
    else:
        path, ts = latest
        print(f"jsonl: {ts}  {path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
