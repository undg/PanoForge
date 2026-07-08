#!/usr/bin/env bash
# Lanceur convivial de PanoForge.
# - Si l'appli tourne déjà : ouvre juste le navigateur.
# - Sinon : démarre le serveur (via run.sh) puis ouvre le navigateur.
set -u

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

URL="http://127.0.0.1:8360/"

if curl -fsS -o /dev/null "http://127.0.0.1:8360/api/config" 2>/dev/null; then
    echo "PanoForge est déjà lancé — ouverture du navigateur."
    command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
    exit 0
fi

exec ./run.sh
