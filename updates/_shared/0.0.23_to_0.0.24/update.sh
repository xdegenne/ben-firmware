#!/usr/bin/env bash
# update.sh — pi-0.0.23 → pi-0.0.24
#
# 1) FIX store SQLite multi-thread (db.py) : la connexion est désormais ouverte
#    avec check_same_thread=False. Le récepteur LoRa ouvrait la connexion dans
#    le thread principal mais écrivait depuis le thread RX → chaque écriture
#    échouait silencieusement ("SQLite objects created in a thread can only be
#    used in that same thread"). Le sink LoRa n'avait JAMAIS écrit une ligne.
#    (Le wired n'était pas touché : il écrit depuis son thread principal.)
# 2) API locale : GET /ping → {"ben":true}, AUCUNE lecture (ni fichier ni base),
#    pour du polling régulier (voyant de joignabilité de l'app).
# 3) GET /health renvoie en plus `last_tic_ts` (ts de la dernière trame TIC).
#
# Code déjà sur disque après `git checkout pi-0.0.24`. On redémarre l'API ET le
# reader (db.py impacte les deux process). Pas de reboot.
#
# Tourne en `ben` ; sudo pour les restarts. Idempotent.

set -euo pipefail

TR="pi-0.0.23 → pi-0.0.24"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
DEVICE_JSON="/etc/ben-firmware/device.json"

MODEL=$(python3 -c "import json;print(json.load(open('$DEVICE_JSON'))['model'])") \
    || fail "lecture du model depuis device.json"
log "[1/2] modèle = $MODEL"

log "[2/2] restart ben-local-api + reader (fix sink SQLite multi-thread)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
case "$MODEL" in
    pi0-wired)       sudo systemctl restart ben-tic-reader.service     || fail "restart tic-reader" ;;
    pi0-lora)        sudo systemctl restart ben-lora-receiver.service  || fail "restart lora-receiver" ;;
    pi0-lora-wired)  sudo systemctl restart ben-tic-reader.service     || fail "restart tic-reader" ;;
    *)               log "modèle sans reader connu — skip restart reader" ;;
esac

log "✓ update OK — sink SQLite réparé (écriture cross-thread) + /ping + last_tic_ts"
