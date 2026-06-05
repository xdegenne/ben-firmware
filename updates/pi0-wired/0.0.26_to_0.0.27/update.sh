#!/usr/bin/env bash
# update.sh — pi-0.0.26 → pi-0.0.27
#
# Vérification couleur au provisioning BLE (anti-association au mauvais boîtier).
#   - led.py  : palette daltonien (B/J/Blanc/Rouge, sans vert) + start_sequence
#               (affichage d'une séquence de couleurs).
#   - main.py : caractéristiques GATT VERIFY / VERIFY_STATUS, hook on_connect qui
#               génère un code couleur affiché sur la LED, et WIFI_CONFIG refusé
#               tant que la vérification n'est pas passée (failed:not_verified).
#   Cf. docs/ble-color-verification.md
#
# ⚠ CHANGEMENT CASSANT : il faut l'app avec l'étape VERIFY pour provisionner.
#   N'impacte QUE le provisioning BLE — un device déjà en service (mode normal)
#   n'est pas affecté. Le ben-ble-provisioner est on-demand (pas un service
#   permanent) et check_network est one-shot au boot : le nouveau code est pris
#   au prochain provisioning / boot. Rien à redémarrer ici.
#
# Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.26 → pi-0.0.27"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

[ -f "$REPO/src/pi/provisioner/main.py" ] || fail "manquant : provisioner/main.py"
[ -f "$REPO/src/pi/provisioner/led.py" ]  || fail "manquant : provisioner/led.py"
# Garde-fou : le code de vérification doit bien être présent (checkout OK).
grep -q "VERIFY_UUID" "$REPO/src/pi/provisioner/main.py" \
    || fail "code de vérification absent de main.py (checkout incomplet ?)"
grep -q "VERIFY_PALETTE" "$REPO/src/pi/provisioner/led.py" \
    || fail "palette de vérification absente de led.py"

log "✓ update OK — vérification couleur en place (effet au prochain provisioning BLE)"
