#!/usr/bin/env bash
# update.sh — pi-0.0.39 → pi-0.0.41   (pi0-lora uniquement)
#
# Courbe LoRa v0x04 : le récepteur décode désormais la trame v0x04 (courbe PAPP
# batchée ~2 s, keyframe + deltas, HMAC-8) EN PLUS de v0x02/v0x01, et réutilise
# l'infra courbe arrivée en 0.0.40 (db.py curve_buckets + local_api.py /curve).
# On saute 0.0.40 (= tag wired). Touche :
#  - store/db.py             : record_measurements_batch + curve_buckets (de 0.0.40)
#  - store/local_api.py      : /curve agrégé, /measurements degrade-safe (de 0.0.40)
#  - lora-receiver/curve_codec.py + main.py : décodage v0x04
#
# AUCUN changement de schéma → AUCUNE migration de données. Il faut juste
# RECHARGER le code dans les process → restart des deux services. Code déjà sur
# disque après `git checkout pi-0.0.41`. Tourne en `ben`. Idempotent.
#
# ⚠️ ORDRE : déployer ce Pi AVANT de reflasher l'Arduino en 0.0.2. Un Pi resté
# en 0.0.39 rejette le v0x04 (perte de données en attendant).

set -euo pipefail

TR="pi-0.0.39 → pi-0.0.41"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a bien amené le nouveau code ?
grep -q "def curve_buckets" "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (checkout incomplet ?)"
grep -q "import curve_codec" "$REPO/src/pi/lora-receiver/main.py" \
    || fail "main.py lora pas à jour (checkout incomplet ?)"
[ -f "$REPO/src/pi/lora-receiver/curve_codec.py" ] \
    || fail "curve_codec.py absent (checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (décodage v0x04 + /curve)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — courbe LoRa v0x04 : récepteur décode v0x04, /curve agrégé"
