#!/usr/bin/env bash
# User-friendly PanoForge launcher.
# - If the app is already running: just open the browser.
# - Otherwise: start the server (via run.sh) then open the browser.
set -u

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

URL="http://127.0.0.1:8360/"

if curl -fsS -o /dev/null "http://127.0.0.1:8360/api/config" 2>/dev/null; then
    echo "PanoForge is already running — opening the browser."
    command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
    exit 0
fi

exec ./run.sh
