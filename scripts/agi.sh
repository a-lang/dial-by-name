#!/usr/bin/env bash
# dial-by-name FastAGI 的手動啟停（PoC 用；正式部署請用 systemd）
#
#   scripts/agi.sh foreground     前景執行，Ctrl+C 停止（看即時 log）
#   scripts/agi.sh start          背景啟動，PID 寫 out/agi.pid、log 寫 out/agi.log
#   scripts/agi.sh stop           依 PID 停止
#   scripts/agi.sh restart
#   scripts/agi.sh reload         送 SIGHUP → 等 reload ok/failed；失敗回非零
#   scripts/agi.sh status         行程＋port＋sweeper heartbeat＋active session＋最近 JSONL
#
# 環境變數：
#   AGI_LISTEN        預設 127.0.0.1:4573
#   AGI_LOGS          預設 out/calls
#   AGI_HEALTH        sweeper heartbeat，預設 out/agi.health.json
#   AGI_PBX_DB        設為 1 → userman 名冊（需 AGI_SOURCE_DIRECTORY）
#   AGI_SOURCE_DIRECTORY  userman_directories.name（例：PBX Internal Directory）
#   AGI_DIRECTORY     名冊 JSON，預設 data/people.json（AGI_PBX_DB=0 時）
#   AGI_ALIASES       aliases.json，預設 data/aliases.json（userman 模式）
#   AGI_DEPARTMENTS   部門定義，預設 data/departments.json（userman 模式）
# 其餘參數會原樣傳給 agi_server.py（例：scripts/agi.sh start --listen 127.0.0.1:4573）

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

VENV_PY="$ROOT/.venv/bin/python"
# .env 必須在下面抓 AGI_* 變數「之前」載入，否則 wrapper 讀不到 .env 的值
# （2026-11-24 修：原本在 case 分支內才 source，AGI_PBX_DB／AGI_SOURCE_DIRECTORY
# 放 .env 會靜默失效、服務掉回 JSON 名冊。注意：.env 值會蓋掉 inline 環境變數）。
load_env() {
  if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
}
load_env

PID_FILE="$ROOT/out/agi.pid"
LOG_FILE="$ROOT/out/agi.log"
LISTEN="${AGI_LISTEN:-127.0.0.1:4573}"
LOGS="${AGI_LOGS:-out/calls}"
HEALTH_FILE="${AGI_HEALTH:-out/agi.health.json}"
DIRECTORY="${AGI_DIRECTORY:-data/people.json}"
PBX_DB="${AGI_PBX_DB:-}"
SOURCE_DIRECTORY="${AGI_SOURCE_DIRECTORY:-}"
ALIASES="${AGI_ALIASES:-data/aliases.json}"
DEPARTMENTS="${AGI_DEPARTMENTS:-data/departments.json}"
PORT="${LISTEN##*:}"

usage() {
  echo "usage: scripts/agi.sh {foreground|start|stop|restart|reload|status} [agi_server args...]" >&2
}

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

server_args() {
  local -a mode_args
  # userman 模式：AGI_PBX_DB=1 或直接傳 --pbx-db；否則沿用 JSON 名冊（回滾＝這裡切回）
  if [[ "$PBX_DB" == "1" || " $* " == *" --pbx-db "* ]]; then
    mode_args=(--pbx-db)
    [[ -n "$SOURCE_DIRECTORY" ]] && mode_args+=(--source-directory "$SOURCE_DIRECTORY")
    mode_args+=(--aliases "$ALIASES" --departments "$DEPARTMENTS")
  else
    mode_args=(--directory "$DIRECTORY")
  fi
  printf '%s\n' "$VENV_PY" agi_server.py \
    --listen "$LISTEN" \
    "${mode_args[@]}" \
    --logs "$LOGS" \
    --health "$HEALTH_FILE" \
    "$@"
}

require_venv() {
  if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 $VENV_PY；請先執行 uv sync" >&2
    exit 1
  fi
}

cmd=${1:-start}
shift || true

case "$cmd" in
  foreground)
    require_venv
    mapfile -t args < <(server_args "$@")
    exec "${args[@]}"
    ;;

  start)
    if is_running; then
      echo "已在執行（pid $(cat "$PID_FILE")）；要重啟請用 restart" >&2
      exit 0
    fi
    rm -f "$PID_FILE"
    require_venv
    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$LOGS"
    mapfile -t args < <(server_args "$@")
    nohup "${args[@]}" >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 0.5
    if ! is_running; then
      echo "啟動失敗；log 最後 20 行：" >&2
      tail -n 20 "$LOG_FILE" >&2 || true
      rm -f "$PID_FILE"
      exit 1
    fi
    echo "started pid $(cat "$PID_FILE")  listen $LISTEN  log $LOG_FILE"
    ;;

  stop)
    if ! is_running; then
      echo "沒有在執行（PID 檔不存在或程序已結束）"
      rm -f "$PID_FILE"
      exit 0
    fi
    pid=$(cat "$PID_FILE")
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "stopped pid $pid"
        exit 0
      fi
      sleep 0.25
    done
    echo "pid $pid 仍在執行；未強制 kill（請自行檢查）" >&2
    exit 1
    ;;

  restart)
    "$0" stop
    exec "$0" start "$@"
    ;;

  reload)
    # 訊號只證明送達；結果以 log 的 reload ok/failed 為準（計畫 §4）
    if ! is_running; then
      echo "沒有在執行；無法 reload（reload 只支援 start 的背景模式）" >&2
      exit 1
    fi
    if [[ ! -f "$LOG_FILE" ]]; then
      echo "找不到 $LOG_FILE；reload 只支援 start 的背景模式（foreground 沒有 log 檔）" >&2
      exit 1
    fi
    pid=$(cat "$PID_FILE")
    before=$(wc -l <"$LOG_FILE")
    kill -HUP "$pid"
    result=""
    for _ in $(seq 1 20); do
      sleep 0.5
      result=$(tail -n +"$((before + 1))" "$LOG_FILE" | grep -E 'reload (ok|failed)' | tail -n 1 || true)
      [[ -n "$result" ]] && break
    done
    if [[ -z "$result" ]]; then
      echo "reload 逾時：10 秒內 log 沒有 reload ok/failed" >&2
      exit 1
    fi
    echo "$result"
    [[ "$result" == *"reload ok"* ]] || exit 1
    ;;

  status)
    rc=0
    pid=""
    if is_running; then
      pid=$(cat "$PID_FILE")
      echo "running: pid $pid  listen $LISTEN"
    else
      echo "stopped（$LISTEN）"
    fi
    if command -v ss >/dev/null 2>&1; then
      ss -ltnp 2>/dev/null | grep ":$PORT " || true
    fi
    # sweeper 是 liveness 的一部分：heartbeat 太久沒更新 = 殭屍 session 永佔並發閘。
    if [[ -x "$VENV_PY" ]]; then
      if [[ -n "$pid" ]]; then
        "$VENV_PY" -m dialname.health --health "$HEALTH_FILE" --pid "$pid" --logs "$LOGS" || rc=1
      else
        "$VENV_PY" -m dialname.health --health "$HEALTH_FILE" --logs "$LOGS" 2>/dev/null \
          | grep '^jsonl:' || true
      fi
    else
      echo "sweeper: unknown（找不到 $VENV_PY；請先 uv sync）" >&2
      if [[ -n "$pid" ]]; then rc=1; fi
    fi
    exit "$rc"
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    usage
    exit 2
    ;;
esac
