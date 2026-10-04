#!/usr/bin/env bash
# Launches PanoForge: creates the venv if needed, installs the dependencies (only if
# necessary; force with PANOFORGE_FORCE_INSTALL=1), starts uvicorn on
# 127.0.0.1:8360 and opens the browser.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR=".venv"
HOST="127.0.0.1"
PORT="8360"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating the venv ($VENV_DIR)..."
    python3 -m venv "$VENV_DIR"
fi

# Conditional install: only reinstalls if the key dependencies are missing
# (or if PANOFORGE_FORCE_INSTALL=1). Otherwise subsequent launches skip the pip step
# — silent and slow — and start uvicorn almost instantly.
if [ "${PANOFORGE_FORCE_INSTALL:-0}" = "1" ] || \
   ! "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, numpy" >/dev/null 2>&1; then
    echo "Installing dependencies (first run / update)..."
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r requirements.txt
else
    echo "Dependencies already installed."
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Warning: ffmpeg not found in PATH. Install it (e.g. sudo apt install ffmpeg)." >&2
fi

URL="http://${HOST}:${PORT}/"

(
    # Let the server start before opening the browser.
    for _ in $(seq 1 30); do
        sleep 0.5
        if curl -fsS -o /dev/null "$URL" 2>/dev/null || curl -fsS -o /dev/null "http://${HOST}:${PORT}/api/config" 2>/dev/null; then
            break
        fi
    done
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || true
    fi
) &

echo "Starting the server on ${URL}"
exec "$VENV_DIR/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT"
