#!/usr/bin/env bash
# update.sh — pi-0.0.43 → pi-0.0.44   (pi0-wired)
#
# LED de boot : ajoute 3 flashs bleus brefs au démarrage (aligné avec le récepteur
# LoRa). Cosmétique pur, AUCUNE migration BDD. Code déjà sur disque après
# `git checkout pi-0.0.44`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.43 → pi-0.0.44"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "3 flashs bleus" "$REPO/src/pi/tic-reader/main_uart.py" \
    || fail "main_uart.py pas à jour (LED boot absente — checkout incomplet ?)"

log "[1/1] restart ben-tic-reader + ben-local-api (LED boot)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"

log "✓ update OK — LED boot : 3 flashs bleus"
