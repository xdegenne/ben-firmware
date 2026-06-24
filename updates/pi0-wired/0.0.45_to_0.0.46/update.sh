#!/usr/bin/env bash
# update.sh — pi-0.0.45 → pi-0.0.46   (pi0-wired)
#
# Alignement NO-OP avec pi0-lora 0.0.46 (trame courbe LoRa v0x05). `curve_codec.py` n'est
# utilisé QUE par le récepteur LoRa → AUCUN impact fonctionnel sur le wired, qui lit la TIC
# en direct (pas de trame LoRa). On bump pour garder les deux modèles + INITIAL_TAG alignés.
#
# AUCUNE migration BDD. Code déjà sur disque après `git checkout pi-0.0.46`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.45 → pi-0.0.46"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout pi-0.0.46 est-il bien en place ? (marqueur du tag dans curve_codec)
grep -q "PROTOCOL_VERSION_CURVE = 0x05" "$REPO/src/pi/lora-receiver/curve_codec.py" \
    || fail "checkout pi-0.0.46 incomplet (marqueur v0x05 absent)"

log "[1/1] restart ben-tic-reader + ben-local-api (alignement no-op v0x05)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"

log "✓ update OK — wired aligné pi-0.0.46 (no-op : v0x05 LoRa sans effet sur le wired)"
