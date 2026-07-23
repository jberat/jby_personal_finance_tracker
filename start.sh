#!/bin/bash
# Personal Financial Tracker — startup with error logging (port 5005).
# Works from anywhere — resolves the app folder from this script's location.

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=5005
LOG="$APP_DIR/app.log"

# Kill any old instance — match by app.py path AND by anyone holding the
# port, so a stale process can't collide.
pkill -f "$APP_DIR/app.py" 2>/dev/null || true
PORT_PIDS="$(lsof -ti tcp:$PORT 2>/dev/null || true)"
if [ -n "$PORT_PIDS" ]; then
    kill $PORT_PIDS 2>/dev/null || true
    sleep 1
    # If any survived, force.
    PORT_PIDS="$(lsof -ti tcp:$PORT 2>/dev/null || true)"
    [ -n "$PORT_PIDS" ] && kill -9 $PORT_PIDS 2>/dev/null || true
fi
sleep 1

# Find a python3 that has Flask installed
PYTHON=""
for candidate in \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3 \
    "$HOME/Library/Python/3.9/bin/python3" \
    "$HOME/Library/Python/3.11/bin/python3" \
    "$HOME/Library/Python/3.12/bin/python3" \
    /usr/bin/python3 \
    "$(command -v python3 2>/dev/null)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c "import flask" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "$(date): ERROR — no python3 with flask found (pip3 install -r requirements.txt)" >> "$LOG"
    echo "ERROR: no python3 with Flask installed. Run: pip3 install -r requirements.txt"
    exit 1
fi

echo "$(date): Starting with $PYTHON" >> "$LOG"
"$PYTHON" "$APP_DIR/app.py" >> "$LOG" 2>&1 &

sleep 2
echo "Personal Financial Tracker running at http://127.0.0.1:$PORT (log: $LOG)"
command -v open >/dev/null 2>&1 && open "http://127.0.0.1:$PORT"
