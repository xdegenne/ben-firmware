#!/usr/bin/env bash
# update.sh — pi-0.0.31 → pi-0.0.32
#
# Provisioner BLE — feedback à la connexion :
#   à la connexion Bluetooth, on ARRÊTE le blink d'attente violet/jaune et on
#   fait 2 flashs verts rapides (= connecté), puis (après le délai de 10s) la
#   boucle du code couleur démarre.
#
# N'impacte QUE le provisioning BLE. ben-ble-provisioner est on-demand : le
# nouveau code est pris au prochain provisioning. RIEN à redémarrer.
#
# Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.31 → pi-0.0.32"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
MAIN="$REPO/src/pi/provisioner/main.py"

# Garde-fou : le checkout doit bien porter le nouveau code.
grep -q "2 flashs verts puis code couleur" "$MAIN" \
    || fail "feedback connexion (2 flashs verts) absent de main.py (checkout incomplet ?)"

log "✓ update OK — 2 flashs verts à la connexion BLE (effet au prochain provisioning)"
