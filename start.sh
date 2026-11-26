#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8001}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

if [[ $# -gt 1 ]]; then
  echo "用法：./start.sh [amap|baidu]" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then
  case "$1" in
    amap|baidu) export MAP_PROVIDER="$1" ;;
    *) echo "地图只能选择 amap 或 baidu" >&2; exit 2 ;;
  esac
fi

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$PROJECT_DIR/backend"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" &
BACKEND_PID=$!

cd "$PROJECT_DIR/frontend"
VITE_BACKEND_TARGET="http://127.0.0.1:$BACKEND_PORT" npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT"
