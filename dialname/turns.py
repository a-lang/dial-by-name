# SPDX-License-Identifier: AGPL-3.0-or-later
"""一輪一個 session：tail 旁路 wav → Realtime WS；`result` 進門才算收工。

旁路 app 是 R0 定案的 C 案（`MixMonitor(,r(${DBN_WAV}-in.wav))`），所以一輪登記
**兩檔**：side wav（`-in.wav`）與 `Record` wav（`.wav`）。離開路徑（result／TTL）都清這兩檔。

三個關鍵決定（`.plan/DEVLOG.md` R0 §5）：
1. 收工訊號＝`result` 進門，**不是**「檔案停滯 500 ms」——錄音中本來就會停 2.048 s。
2. 送 WS 的 chunk 由 `stt_realtime` 切 ≤1 s；這裡只負責「讀到什麼送什麼」。
3. frames＝0 只能在 result 判定（Stop 後必定至少 flush 一次），worker 不下結論。

`WavTailer` **不跳過前導補靜音**：`r()` 預設補的 300 ms 同步靜音是音檔時間軸的原點，
也是 VAD 的 commit 依據（R0 §4-5 定案）；加了 `n` 反而會壞掉。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .stt_realtime import RealtimeAuthError, RealtimeError, RealtimeStt, RealtimeUnavailable

log = logging.getLogger("dialname.agi")

FRAME_BYTES = 320              # 20 ms @ 8 kHz mono s16
MAX_SESSIONS = 4               # 並發閘（寫死；理由記 DEVLOG，R3 觀察後再議）
TTL_SECONDS = 20.0             # result 沒來的分支（OPERATOR／HANGUP／ERROR）由 sweeper 收
COMMIT_WAIT = 1.0              # EOF 補 commit 後等最後一則 transcript 的上限
RESULT_TIMEOUT = 3.0           # result 端總預算（與 worker 並行，不是串行疊加）
POLL_INTERVAL = 0.02
WORKER_JOIN = 0.5
SIDE_SUFFIX = "-in.wav"
MAX_HEADER_BYTES = 65536


def session_key(call_id: str, seq: str | int) -> str:
    return f"{call_id}:{seq}"


def record_path_for(side_wav: str | Path) -> Path | None:
    """`…-1-in.wav` → `…-1.wav`；不符合旁路命名回 None（只登記 side 檔）。"""
    side = Path(side_wav)
    if not side.name.endswith(SIDE_SUFFIX):
        return None
    return side.with_name(side.name[:-len(SIDE_SUFFIX)] + ".wav")


def wav_data_offset(head: bytes) -> int | None:
    """走 RIFF chunk 找 `data` 起點；錄音中 riff／data size 是 0，只看 chunk 佈局。"""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(head):
        chunk_id = head[pos:pos + 4]
        size = int.from_bytes(head[pos + 4:pos + 8], "little")
        if chunk_id == b"data":
            return pos + 8
        pos = pos + 8 + size + (size & 1)
    return None


class WavTailer:
    """把成長中的 wav 當 PCM 串流讀；檔還沒出現時回 0，不視為錯誤。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = None
        self._offset: int | None = None
        self._consumed = 0

    def pump(self, rt) -> int:
        if self._fh is None:
            try:
                self._fh = open(self.path, "rb")
            except FileNotFoundError:
                return 0
        if self._offset is None:
            head = self._fh.read(MAX_HEADER_BYTES)
            self._fh.seek(0)
            self._offset = wav_data_offset(head)
            if self._offset is None:
                return 0
        self._fh.seek(self._offset + self._consumed)
        data = self._fh.read()
        if not data:
            return 0
        rt.send_pcm(data)
        self._consumed += len(data)
        return len(data)

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()


@dataclass
class TurnSession:
    key: str
    call_id: str
    seq: str
    side_wav: Path
    record_wav: Path | None
    opened: float
    state: str = "created"          # created → streaming → committed → closed
    stub: bool = False
    frames: int = 0
    partials: int = 0
    last_partial: str = ""
    text: str | None = None
    error: str | None = None
    detail: str = ""
    open_ms: int | None = None
    commit_ms: int | None = None
    result_seen: threading.Event = field(default_factory=threading.Event)
    final: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(p for p in (self.side_wav, self.record_wav) if p is not None)

    @property
    def status(self) -> str:
        """JSONL `stream.status`：ok／dropped（有 transcript 的故障）／error／stub。"""
        if self.stub:
            return "stub"
        if self.error:
            return "dropped" if self.text is not None else "error"
        return "ok"


class TurnManager:
    """session 池＋worker＋TTL。池子跨 SIGHUP 存活（reload 只換名冊）。"""

    def __init__(self, connector=None, *, max_sessions: int = MAX_SESSIONS,
                 ttl: float = TTL_SECONDS, commit_wait: float = COMMIT_WAIT,
                 poll_interval: float = POLL_INTERVAL, clock=time.monotonic):
        self._connect = connector or (lambda: RealtimeStt())
        self.max_sessions = max_sessions
        self.ttl = ttl
        self.commit_wait = commit_wait
        self.poll_interval = poll_interval
        self._clock = clock
        self._sessions: dict[str, TurnSession] = {}
        self._lock = threading.Lock()

    # -- 生命週期 -----------------------------------------------------------
    def start(self, call_id: str, seq: str | int, side_wav: str | Path,
              record_wav: str | Path | None = None) -> TurnSession:
        """登記兩檔並背景起 worker；立刻返回，永不拋例外（閘滿／失敗＝stub）。"""
        side = Path(side_wav)
        record = Path(record_wav) if record_wav else record_path_for(side)
        session = TurnSession(
            key=session_key(call_id, seq), call_id=str(call_id), seq=str(seq),
            side_wav=side, record_wav=record, opened=self._clock(),
        )
        with self._lock:
            old = self._sessions.get(session.key)
            if len(self._sessions) >= self.max_sessions:
                session.stub = True
                session.final.set()
            self._sessions[session.key] = session
        if old is not None:
            # 同一輪被 start 兩次：舊的讓路但不清檔（新的一輪正在寫同兩個檔）
            self._abort(old, unlink=False)
        if session.stub:
            return session

        thread = threading.Thread(target=self._run, args=(session,), daemon=True,
                                  name=f"turn-{session.key}")
        session.thread = thread
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001 — 起不了 worker 也不能讓 result 永遠等
            log.warning("turn worker 起不來：%s（%s）", session.key, exc)
            session.error = "internal"
            session.detail = str(exc)
            session.final.set()
        return session

    def claim(self, call_id: str, seq: str | int) -> TurnSession | None:
        """result 取走 session（同一個 key 只會被取一次）。"""
        key = session_key(call_id, seq)
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            session.result_seen.set()
        return session

    def finish(self, session: TurnSession) -> None:
        """等 worker 收尾（已結束就立即返回）並刪掉登記的兩檔。"""
        self._abort(session, unlink=True)

    def sweep(self) -> list[str]:
        """TTL 到期：close WS＋unlink。回傳被清掉的 key（給 log／健康檢查用）。"""
        now = self._clock()
        with self._lock:
            expired = [s for s in self._sessions.values() if now - s.opened >= self.ttl]
            for session in expired:
                self._sessions.pop(session.key, None)
        for session in expired:
            self._abort(session, unlink=True)
        return [s.key for s in expired]

    def active(self) -> int:
        with self._lock:
            return len(self._sessions)

    # -- 內部 ---------------------------------------------------------------
    def _abort(self, session: TurnSession, *, unlink: bool) -> None:
        session.closed.set()
        thread = session.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(WORKER_JOIN)
        if not session.final.is_set() and not session.stub:
            # worker 沒在 join 時間內收尾（例如卡在 open）：記成逾時，log 才說得出原因
            session.error = session.error or "timeout"
        if unlink:
            for path in session.paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    # 隱私清理失敗要留痕，但不讓通話卡住
                    log.warning("清理 wav 失敗：%s（%s）", path, exc)
        session.state = "closed"
        session.final.set()

    def _elapsed_ms(self, session: TurnSession) -> int:
        return int((self._clock() - session.opened) * 1000)

    def _consume(self, session: TurnSession, events) -> None:
        for event in events:
            if event.kind == "partial":
                # 空 partial 只代表「這一段沒語音」（keepalive 也是空字串），不算講過話
                if event.text.strip():
                    session.partials += 1
                    session.last_partial = event.text
            elif event.kind == "commit":
                session.text = (session.text or "") + event.text
                if session.commit_ms is None:
                    session.commit_ms = self._elapsed_ms(session)
            elif event.kind == "error":
                session.error = session.error or "ws"
                session.detail = event.detail
            elif event.kind == "closed":
                session.error = session.error or "dropped"
                session.detail = event.detail

    def _pump(self, session: TurnSession, tail: WavTailer, rt) -> int:
        sent = tail.pump(rt)
        session.frames += sent // FRAME_BYTES
        self._consume(session, rt.poll(self.poll_interval))
        return sent

    def _run(self, session: TurnSession) -> None:
        rt = None
        tail = None
        try:
            rt = self._connect()
            rt.open()
            session.open_ms = self._elapsed_ms(session)
            session.state = "streaming"
            tail = WavTailer(session.side_wav)
            while not session.result_seen.is_set() and not session.closed.is_set():
                if self._pump(session, tail, rt) == 0:
                    time.sleep(self.poll_interval)
            if not session.closed.is_set():
                # StopMixMonitor 已 flush；把剩下的讀完，補一則 commit 當 EOF 收尾
                self._pump(session, tail, rt)
                rt.send_pcm(b"", commit=True)
                deadline = time.monotonic() + self.commit_wait
                while (session.text is None and not session.closed.is_set()
                       and time.monotonic() < deadline):
                    self._consume(session, rt.poll(self.poll_interval))
                self._consume(session, rt.poll(0.0))
                session.state = "committed"
        except RealtimeAuthError as exc:
            session.error = "auth"
            session.detail = str(exc)
        except RealtimeUnavailable as exc:
            session.error = "unavailable"
            session.detail = str(exc)
        except RealtimeError as exc:
            session.error = "ws"
            session.detail = str(exc)
        except Exception as exc:  # noqa: BLE001 — worker 死掉不能讓 result 永遠等
            session.error = "internal"
            session.detail = f"{type(exc).__name__}: {exc}"
        finally:
            if tail is not None:
                tail.close()
            if rt is not None:
                rt.close()
            session.state = "closed"
            session.final.set()
