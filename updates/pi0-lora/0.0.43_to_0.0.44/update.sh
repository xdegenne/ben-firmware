#!/usr/bin/env bash
# update.sh — pi-0.0.43 → pi-0.0.44   (pi0-lora)
#
# LED de boot : remplace l'ancienne séquence arc-en-ciel "disco" (~5 s, 9 couleurs)
# par 3 flashs bleus brefs au démarrage (aligné avec le wired). Cosmétique pur,
# AUCUNE migration BDD. Code déjà sur disque après `git checkout pi-0.0.44`.
# Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.43 → pi-0.0.44"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "3 flashs bleus" "$REPO/src/pi/lora-receiver/main.py" \
    || fail "main.py pas à jour (LED boot absente — checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (LED boot)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — LED boot : 3 flashs bleus (séquence disco supprimée)"
