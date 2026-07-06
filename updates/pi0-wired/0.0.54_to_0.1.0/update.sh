#!/usr/bin/env bash
# update.sh — pi-0.0.54 → pi-0.1.0   (pi0-wired)
#
# Palier 0.1.0 (fin des 0.0.x). Lecteur wired (main_uart.py) : ALIGNEMENT du logging avec le
# récepteur LoRa — les champs collectés-mais-non-stockés sont logués INFO ON-CHANGE via
# log_uncabled (même format que le LoRa) : DEMAIN/ADPS/PEJP (histo) + PARSE & log NJOURF/NJOURF+1
# (Tempo standard, nouveau — n'étaient pas parsés).
# STRICTEMENT ADDITIF : aucune migration BDD, aucun champ API retiré/renommé.
# Code déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.54 → pi-0.1.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'def log_uncabled' "$REPO/src/pi/tic-reader/main_uart.py" \
    || fail "main_uart.py pas à jour (log_uncabled absent — checkout incomplet ?)"
log "restart ben-tic-reader (logging aligné + parse NJOURF) + ben-local-api"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"
log "✓ update OK — palier 0.1.0 (logging aligné LoRa/wired)"
