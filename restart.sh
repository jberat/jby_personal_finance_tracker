#!/bin/bash
# Restart Personal Financial Tracker (port 5005).
# Works from anywhere — resolves the app folder from this script's location.
# Handy as the shell script inside an Automator "Run Shell Script" action.

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=5005

# Kill any old instance — match by app.py path AND by anyone holding the port.
pkill -f "$APP_DIR/app.py" 2>/dev/null || true
PORT_PIDS="$(lsof -ti tcp:$PORT 2>/dev/null || true)"
if [ -n "$PORT_PIDS" ]; then
    kill $PORT_PIDS 2>/dev/null || true
    sleep 1
    PORT_PIDS="$(lsof -ti tcp:$PORT 2>/dev/null || true)"
    [ -n "$PORT_PIDS" ] && kill -9 $PORT_PIDS 2>/dev/null || true
fi
sleep 1

python3 "$APP_DIR/app.py" &
sleep 2
echo "Personal Financial Tracker running at http://127.0.0.1:$PORT"
command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT"
