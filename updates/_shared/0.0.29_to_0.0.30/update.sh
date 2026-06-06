#!/usr/bin/env bash
# update.sh — pi-0.0.29 → pi-0.0.30
#
# tic-reader (wired) :
#   - wake-up LED passe de jaune à bleu (cosmétique).
#   - PERIOD_S 30s → 15s (lecture TIC toutes les 15s).
#
# Le code est lu depuis /opt/ben/repo au démarrage du process → déjà sur disque
# après le git checkout. Il suffit de relancer tic-reader POUR qu'il prenne le
# nouveau code. On utilise `try-restart` : il relance le service UNIQUEMENT s'il
# tourne déjà (mode normal). En mode provisioning il est arrêté → try-restart
# est un no-op → on ne risque pas de le démarrer et de tuer ben-ble-provisioner.
#
# Tourne en `ben` (sudo pour systemctl). Idempotent.

set -euo pipefail

TR="pi-0.0.29 → pi-0.0.30"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
MAIN="$REPO/src/pi/tic-reader/main_uart.py"

# Garde-fous : le checkout doit bien porter le nouveau code.
grep -q "PERIOD_S           = 15" "$MAIN" \
    || fail "PERIOD_S=15 absent de main_uart.py (checkout incomplet ?)"
grep -q "blink_rgb(0, 0, 5, 0.05)" "$MAIN" \
    || fail "wake-up bleu absent de main_uart.py (checkout incomplet ?)"

log "[1/1] try-restart ben-tic-reader (applique le nouveau code s'il tourne)"
sudo systemctl try-restart ben-tic-reader.service

log "✓ update OK — wake-up bleu + lecture TIC 15s (effet immédiat si actif, sinon au prochain démarrage)"
