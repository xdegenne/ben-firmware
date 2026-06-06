#!/usr/bin/env bash
# update.sh — pi-0.0.32 → pi-0.0.33
#
# Provisioner BLE — feedback succès connexion réseau :
#   sur succès de la connexion WiFi, 2 flashs verts rapides (au lieu de 3 flashs
#   + vert tenu permanent). Cohérent avec le reste de l'UX (plus de vert fixe).
#
# N'impacte QUE le provisioning BLE. ben-ble-provisioner est on-demand : le
# nouveau code est pris au prochain provisioning. RIEN à redémarrer.
#
# Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.32 → pi-0.0.33"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
MAIN="$REPO/src/pi/provisioner/main.py"

# Garde-fou : le checkout doit bien porter le nouveau code.
grep -q "Succès connexion réseau : 2 flashs verts" "$MAIN" \
    || fail "feedback succès réseau (2 flashs verts) absent de main.py (checkout incomplet ?)"

log "✓ update OK — 2 flashs verts rapides au succès réseau (effet au prochain provisioning)"
