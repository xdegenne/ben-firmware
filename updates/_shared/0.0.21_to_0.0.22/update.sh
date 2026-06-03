#!/usr/bin/env bash
# update.sh — pi-0.0.21 → pi-0.0.22
#
# Phase 1 — store local SQLite + API locale pour l'app Flutter :
#   - Le nouveau code (src/pi/store/{db,local_api}.py + sink dans les readers)
#     est déjà sur disque après le `git checkout pi-0.0.22` fait par l'updater.
#   - Installe + active `ben-local-api.service` (API LAN read-only sur :8087).
#   - Redémarre le reader du modèle pour qu'il charge le sink (le process
#     tournait encore sur l'ancien code, sans stockage).
#
# Tourne en tant que `ben` ; `sudo` pour les opérations privilégiées.
# ⚠ JAMAIS de redirection `sudo cmd > fichier` : le `>` s'exécute en `ben`
#    (bug historique pi-0.0.15). Ici on n'en a pas besoin.

set -euo pipefail

TR="pi-0.0.21 → pi-0.0.22"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
DEVICE_JSON="/etc/ben-firmware/device.json"

# 1. Modèle (détermine quel reader redémarrer)
MODEL=$(python3 -c "import json;print(json.load(open('$DEVICE_JSON'))['model'])") \
    || fail "lecture du model depuis device.json"
log "[1/3] modèle = $MODEL"

# 2. Installe + active l'API locale
SRC="$REPO/config/systemd/ben-local-api.service"
[ -f "$SRC" ] || fail "unit manquante : $SRC"
log "[2/3] install + enable ben-local-api.service (:8087)"
sudo install -m 644 -o root -g root "$SRC" /etc/systemd/system/ben-local-api.service \
    || fail "install unit"
sudo systemctl daemon-reload || fail "daemon-reload"
sudo systemctl enable --now ben-local-api.service || fail "enable/start ben-local-api"

# 3. Redémarre le reader pour charger le sink (code pris au checkout)
case "$MODEL" in
    pi0-wired)       READER=ben-tic-reader ;;
    pi0-lora)        READER=ben-lora-receiver ;;
    pi0-lora-wired)  READER=ben-tic-reader ;;   # wired prioritaire
    *)               READER="" ;;
esac
if [ -n "$READER" ]; then
    log "[3/3] restart $READER (chargement du sink store)"
    sudo systemctl restart "$READER" || fail "restart $READER"
else
    log "[3/3] modèle sans reader connu — skip restart"
fi

log "✓ update OK — store local SQLite + API :8087 actifs"
