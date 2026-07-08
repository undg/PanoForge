#!/usr/bin/env bash
# Lance PanoForge : crée le venv si besoin, installe les dépendances (seulement si
# nécessaire ; forcer avec PANOFORGE_FORCE_INSTALL=1), démarre uvicorn sur
# 127.0.0.1:8360 et ouvre le navigateur.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR=".venv"
HOST="127.0.0.1"
PORT="8360"

if [ ! -d "$VENV_DIR" ]; then
    echo "Création du venv ($VENV_DIR)..."
    python3 -m venv "$VENV_DIR"
fi

# Installation conditionnelle : ne réinstalle que si les dépendances clés manquent
# (ou si PANOFORGE_FORCE_INSTALL=1). Sinon les démarrages suivants sautent l'étape pip
# — silencieuse et lente — et lancent uvicorn quasi instantanément.
if [ "${PANOFORGE_FORCE_INSTALL:-0}" = "1" ] || \
   ! "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, numpy" >/dev/null 2>&1; then
    echo "Installation des dépendances (première fois / mise à jour)..."
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r requirements.txt
else
    echo "Dépendances déjà installées."
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Attention : ffmpeg introuvable dans le PATH. Installez-le (ex: sudo apt install ffmpeg)." >&2
fi

URL="http://${HOST}:${PORT}/"

(
    # Laisse le serveur démarrer avant d'ouvrir le navigateur.
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

echo "Démarrage du serveur sur ${URL}"
exec "$VENV_DIR/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT"
