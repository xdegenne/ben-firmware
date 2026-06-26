#!/usr/bin/env bash
# update.sh — pi-0.0.46 → pi-0.0.47   (pi0-lora)
#
# Désappairage : POST /unprovision ÉTEINT TOUJOURS le boîtier (poweroff), avec ou sans
# wipe (avant : reboot quand pas de wipe). Au prochain allumage, sans WiFi → démarrage en
# mode configuration (BLE). Signal d'extinction clair et uniforme.
#
# Modif local_api.py uniquement. AUCUNE migration BDD. Code déjà sur disque après
# `git checkout pi-0.0.47`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.46 → pi-0.0.47"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout pi-0.0.47 a-t-il bien amené le poweroff inconditionnel ?
grep -q 'poweroff (wipe=' "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (poweroff inconditionnel absent — checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (désappairage = poweroff systématique)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — le désappairage éteint désormais toujours le boîtier"
