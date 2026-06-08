#!/usr/bin/env bash
# update.sh — pi-0.0.35 → pi-0.0.36
#
# 1. Palette de vérif BLE recalibrée (led.py) : BLANC CHAUD (canal bleu écrasé)
#    pour ne plus être confondu avec le bleu sur la LED RGB (confusion constatée).
#    Le provisioner est on-demand → la nouvelle palette est prise au prochain
#    provisioning ; rien à redémarrer pour ça.
# 2. API locale : POST /unprovision (désappairage) — supprime la connexion WiFi
#    `ben-provisioned` (→ provisioning BLE au reboot), efface optionnellement les
#    données (?wipe=1), puis reboot. Garde l'identité (certs, deviceId).
#    → restart ben-local-api pour charger l'endpoint.
#
# Code sur disque après `git checkout pi-0.0.36`. Pas de reboot ici. Tourne en
# `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.35 → pi-0.0.36"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "unprovision" "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (checkout incomplet ?)"
grep -q "Blanc CHAUD" "$REPO/src/pi/provisioner/led.py" \
    || fail "led.py pas à jour (checkout incomplet ?)"

log "[1/1] restart ben-local-api (charge l'endpoint /unprovision)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"

log "✓ update OK — palette LED recalibrée (blanc chaud) + POST /unprovision"
