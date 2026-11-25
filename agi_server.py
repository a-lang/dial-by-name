# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAGI 進入點：listen 127.0.0.1:4573，兩相協議（start／result）＋TTL sweeper。

只 import `dialname` 套件內的模組（AGENTS.md 契約）。

`agi_arg_1` 是動詞：
  start   `AGI(…,start,${DBN_WAV}-in.wav,${UNIQUEID},${DBN_SEQ})`
          登記旁路檔＋Record 檔、背景起 tailer＋WS，立即返回；**不寫任何通道變數**。
  result  `AGI(…,result,${DBN_WAV}.wav,${UNIQUEID},${DBN_SEQ},${DBN_ATTEMPT},${DBN_CANDS})`
          等 transcript（≤3s）→ decide → 六個 `DBN_*`；同時 close WS、刪登記的兩檔。

名冊兩種入口：
  --directory data/people.json           JSON 名冊（PoC／回滾）
  --pbx-db --source-directory <name>     FreePBX userman ＋ aliases.json（正式）

SIGHUP ＝ 原子重載：signal handler 只設 Event，watcher thread 重建 `(Directory, handlers)`
後一次換掉 `state.current`。處理中的通話用舊索引、新進的用新索引；重載失敗保留舊索引。
`TurnManager` 只有一個實例，**session 池跨 reload 存活**（重載只換名冊，不重置進行中 session）。

sweeper 是背景執行緒：每 `SWEEP_INTERVAL` 秒跑 `TurnManager.sweep()`（過期 session 收 WS、
unlink 兩檔）並寫 heartbeat（`dialname.health`）。sweeper 死掉時主行程與 port 都還在，
所以 `scripts/agi.sh status` 以 heartbeat 新舊判定存活，不看行程本身。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Callable

from dialname.agi import AgiSession, make_result_handler, make_start_handler, run_session
from dialname.core import Directory, load_directory
from dialname.health import DEFAULT_HEALTH_PATH, SWEEP_INTERVAL, write_heartbeat
from dialname.pbx_directory import (
    DEFAULT_ALIASES_PATH,
    DEFAULT_DEPARTMENTS_PATH,
    DbConfig,
    load_from_pbx,
    resolve_db_config,
)
from dialname.turns import TurnManager

log = logging.getLogger("dialname.agi")


@dataclass(frozen=True)
class Current:
    """一次載入的完整狀態。`handlers` 綁定同一個 `directory`，必須一起換。"""

    directory: Directory
    handlers: dict[str, Callable[[dict[str, str]], dict | None]]


@dataclass
class State:
    current: Current
    log_dir: str
    build: Callable[[], Current]
    reload_requested: threading.Event = field(default_factory=threading.Event)


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    wbufsize = 0          # 不緩衝；AgiSession 自己 flush
    timeout = 25          # socket idle 與 handler deadline 共用

    def handle(self) -> None:
        self.connection.settimeout(self.timeout)
        session = AgiSession(self.rfile, self.wfile)
        current = self.server.state.current   # 整通電話用同一份索引（重載不影響進行中的通話）

        def worker() -> None:
            try:
                run_session(session, handlers=current.handlers)
            except Exception:  # noqa: BLE001 — 連線層錯誤只記 log，不影響其他通話
                log.exception("AGI session 失敗")

        # settimeout 只包 socket I/O；result 可能卡在 transcript 等待，所以另外用 thread deadline。
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            log.error("AGI handler 超過 %s 秒，關閉連線（dialplan 應走 AGISTATUS 安全網）", self.timeout)
            try:
                self.connection.close()
            except OSError:
                pass


class Sweeper(threading.Thread):
    """TTL sweeper＋heartbeat。

    不吞例外：sweep 壞掉就讓執行緒死，heartbeat 停止更新，status 會判 DEAD——
    比「默默活著但不收尾」可觀測（sweeper 死＝殭屍 session 永佔並發閘）。
    """

    def __init__(self, manager: TurnManager, health_path: str | os.PathLike,
                 *, interval: float = SWEEP_INTERVAL, pid: int | None = None):
        super().__init__(daemon=True, name="turn-sweeper")
        self.manager = manager
        self.health_path = health_path
        self.interval = interval
        self.pid = os.getpid() if pid is None else pid
        self._halt = threading.Event()

    def run(self) -> None:
        write_heartbeat(self.health_path, pid=self.pid, active=self.manager.active(),
                        interval=self.interval)
        while not self._halt.is_set():
            cleared = self.manager.sweep()
            if cleared:
                log.info("sweeper cleared %d expired session(s): %s",
                         len(cleared), ", ".join(cleared))
            write_heartbeat(self.health_path, pid=self.pid, active=self.manager.active(),
                            interval=self.interval)
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()


def make_current(directory: Directory, *, manager: TurnManager, log_dir: str) -> Current:
    return Current(
        directory=directory,
        handlers={
            "start": make_start_handler(manager=manager),
            "result": make_result_handler(manager=manager, directory=directory, log_dir=log_dir),
        },
    )


def apply_reload(state: State) -> bool:
    """重建並原子換掉 `state.current`；失敗保留舊索引（啟動失敗才拒絕起動）。"""
    try:
        new = state.build()
    except Exception as exc:  # noqa: BLE001 — 重載失敗是「這次更新不能用」，不是服務死亡
        # 一行一筆：`scripts/agi.sh reload` 靠 grep `reload ok|reload failed` 判定結果。
        log.error("reload failed: %s (keeping previous index)", str(exc).replace("\n", " | "))
        return False
    old = state.current
    state.current = new
    log.info(
        "reload ok: people=%d aliases=%d (was %d)",
        len(new.directory.people),
        _alias_count(new.directory),
        _alias_count(old.directory),
    )
    return True


def _alias_count(directory: Directory) -> int:
    return sum(len(p.aliases) for p in directory.people)


def reload_watcher(state: State) -> None:
    while True:
        state.reload_requested.wait()
        state.reload_requested.clear()
        apply_reload(state)


def make_server(host: str, port: int, state: State) -> ThreadedServer:
    server = ThreadedServer((host, port), Handler)
    server.state = state
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="dial-by-name FastAGI server")
    ap.add_argument("--listen", default="127.0.0.1:4573", help="host:port")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--directory", help="名冊 JSON（預設 data/people.json）")
    mode.add_argument("--pbx-db", action="store_true", help="從 FreePBX userman 讀名冊（需 --source-directory）")
    ap.add_argument("--source-directory", help="userman_directories.name（--pbx-db 必填）")
    ap.add_argument("--aliases", help=f"aliases.json（僅 --pbx-db；預設 {DEFAULT_ALIASES_PATH}）")
    ap.add_argument("--departments", help=f"部門定義（僅 --pbx-db；預設 {DEFAULT_DEPARTMENTS_PATH}）")
    ap.add_argument("--logs", default="out/calls", help="逐輪 JSONL 目錄")
    ap.add_argument("--health", default=DEFAULT_HEALTH_PATH,
                    help=f"sweeper heartbeat（給 scripts/agi.sh status；預設 {DEFAULT_HEALTH_PATH}）")
    args = ap.parse_args(argv)

    if args.pbx_db and not args.source_directory:
        ap.error("--pbx-db 需要 --source-directory <userman_directories.name>")
    if args.source_directory and not args.pbx_db:
        ap.error("--source-directory 僅適用 --pbx-db")
    if not args.pbx_db and (args.aliases or args.departments):
        ap.error("--aliases／--departments 僅適用 --pbx-db")
    if not args.pbx_db:
        args.directory = args.directory or "data/people.json"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host, _, port = args.listen.rpartition(":")
    if not host:
        ap.error("--listen 需要 host:port")

    manager = TurnManager()

    if args.pbx_db:
        try:
            # 連線來源啟動時定案，reload 沿用同一組（.env 不會在 process 內變動）
            db: DbConfig = resolve_db_config()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        departments = args.departments or DEFAULT_DEPARTMENTS_PATH
        aliases = args.aliases or DEFAULT_ALIASES_PATH

        def build() -> Current:
            directory = load_from_pbx(
                args.source_directory,
                db=db,
                departments_path=departments,
                aliases_path=aliases,
            )
            return make_current(directory, manager=manager, log_dir=args.logs)
    else:

        def build() -> Current:
            return make_current(load_directory(args.directory), manager=manager, log_dir=args.logs)

    try:
        current = build()   # 啟動失敗＝拒絕啟動（fail fast）
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"名冊載入失敗：{exc}") from exc

    state = State(current=current, log_dir=args.logs, build=build)
    signal.signal(signal.SIGHUP, lambda *_: state.reload_requested.set())
    threading.Thread(target=reload_watcher, args=(state,), daemon=True, name="reload-watcher").start()
    sweeper = Sweeper(manager, args.health)
    sweeper.start()

    server = make_server(host, int(port), state)
    log.info("FastAGI listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sweeper.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
