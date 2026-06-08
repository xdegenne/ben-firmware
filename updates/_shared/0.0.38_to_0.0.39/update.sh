#!/usr/bin/env bash
# update.sh — pi-0.0.38 → pi-0.0.39
#
# Provisioning BLE — deux corrections UX de la LED :
#  1. Échec WiFi (ex. mauvais mot de passe) : le BLE reste connecté et l'on peut
#     re-saisir → on ne repart PLUS en violet/jaune (« à configurer », trompeur).
#     3 flashs rouges = échec, puis LED éteinte (état « connecté, en attente »).
#  2. Fin de l'apprentissage des couleurs (« Suivant ») → on éteint la LED et on
#     attend VERIFY_AFTER_PREVIEW_SEC (3 s) avant de démarrer le code de test,
#     le temps d'arriver sur l'écran de test sans rater le début.
#
# Le provisioner est ON-DEMAND (ne tourne qu'en mode BLE, lancé par
# check_network) → RIEN à redémarrer : pris au prochain passage en BLE. Pas de
# reboot. Code sur disque après `git checkout pi-0.0.39`. Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.38 → pi-0.0.39"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "VERIFY_AFTER_PREVIEW_SEC" "$REPO/src/pi/provisioner/main.py" \
    || fail "main.py pas à jour (checkout incomplet ?)"

log "✓ update OK — LED : plus de violet/jaune sur échec WiFi + délai avant le code"
log "  de test. Provisioner on-demand → rien à redémarrer (pris au prochain BLE)."
