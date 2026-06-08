#!/usr/bin/env bash
# update.sh — pi-0.0.34 → pi-0.0.35
#
# /health expose `lastUpdateTs` = date de la dernière MAJ firmware.
#   - local_api.py : _device_info() ajoute `lastUpdateTs` (epoch s), lu via la
#     mtime de /etc/ben-firmware/device.json (réécrit seulement à un changement
#     de version OTA ou au provisioning). Pas de champ stocké, pas de migration.
#   → l'app peut afficher « Mis à jour le … » ; on repère un device en retard.
#
# Code sur disque après `git checkout pi-0.0.35`. Restart ben-local-api pour le
# charger. Pas de reboot, aucun reader touché. Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.34 → pi-0.0.35"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "lastUpdateTs" "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (checkout incomplet ?)"

log "[1/1] restart ben-local-api (expose lastUpdateTs dans /health)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"

log "✓ update OK — /health expose lastUpdateTs"
