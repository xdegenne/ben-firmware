#!/usr/bin/env bash
# update.sh — pi-0.0.42 → pi-0.0.43   (pi0-lora)
#
# Mode TIC STANDARD bout-en-bout côté récepteur :
#   - curve_codec décode le v0x04 standard : papp NET SIGNÉ (src_standard → int16,
#     négatif = injection), bloc extension EAIT (énergie injectée), et l'horodate
#     compteur → meter_ts (immunisée au délai de transmission LoRa).
#   - main.py range la clé GÉNÉRIQUE (src_standard, index_id, index_value) +
#     inject_total + meter_ts ; NTARF standard n'est PLUS mappé sur INDEX_NAMES
#     (histo). /live expose `tic_mode` (standard/historique) pour l'app.
#   - Suppression du watchdog « relance si pas de trame » (débile côté récepteur :
#     pas de trame = normal). La fraîcheur reste signalée par le heartbeat LED.
#
# Migration BDD = ALTER conditionnel (colonnes measurements.src_standard / index_id
# / index_value / inject_total / meter_ts), exécuté automatiquement au prochain
# db.connect() (démarrage des services). Idempotente, NON bloquante (ADD COLUMN
# SQLite instantané), double-écriture base/hchc/hchp préservée. Code déjà sur
# disque après `git checkout pi-0.0.43`. Tourne en `ben`.
#
# ⚠️ ORDRE : déployer ce Pi AVANT de flasher l'Arduino en standard (arduino 0.0.4) —
# un récepteur 0.0.42 misread une trame standard (papp signé lu en uint16).

set -euo pipefail

TR="pi-0.0.42 → pi-0.0.43"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a bien amené le nouveau code ?
grep -q "def meter_epoch" "$REPO/src/pi/lora-receiver/curve_codec.py" \
    || fail "curve_codec.py pas à jour (checkout incomplet ?)"
grep -q "src_standard" "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (migration index bi-mode absente)"

log "[1/1] restart ben-local-api + ben-lora-receiver (décodage standard + migration DB auto)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — récepteur LoRa bi-mode (standard : papp signé, EAIT, meter_ts) ; plus de watchdog restart-sur-silence"
