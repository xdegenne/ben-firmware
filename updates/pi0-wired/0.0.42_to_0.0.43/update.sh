#!/usr/bin/env bash
# update.sh — pi-0.0.42 → pi-0.0.43   (pi0-wired)
#
# Mode TIC STANDARD bout-en-bout côté wired (le compteur peut passer en standard,
# reprogrammé par Enedis) :
#   - main_uart.py est BI-MODE : auto-détection historique↔standard au boot
#     (sonde 1200/9600 via checksum, mode persisté dans tic-state.json, re-détection
#     sur silence prolongé via le watchdog). Parseur standard conforme
#     Enedis-NOI-CPT_54E (séparateur HT, checksum HT-de-queue inclus, horodate).
#   - Stockage GÉNÉRIQUE (chantier index bi-mode) : (src_standard, index_id,
#     index_value) + inject_total (EAIT) + meter_ts (horodate compteur). En standard
#     index_id = NTARF, index_value = EASF[NTARF] ; papp = SINSTS (net signé).
#   - /live expose `tic_mode` (standard/historique) pour l'app.
#
# Migration BDD = ALTER conditionnel ×5 (measurements.src_standard / index_id /
# index_value / inject_total / meter_ts), exécuté automatiquement au prochain
# db.connect(). Idempotente, NON bloquante (ADD COLUMN SQLite instantané),
# double-écriture base/hchc/hchp préservée. Code déjà sur disque après
# `git checkout pi-0.0.43`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.42 → pi-0.0.43"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a bien amené le nouveau code ?
grep -q "def detect_mode" "$REPO/src/pi/tic-reader/main_uart.py" \
    || fail "main_uart.py pas à jour (bi-mode absent — checkout incomplet ?)"
grep -q "src_standard" "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (migration index bi-mode absente)"

log "[1/1] restart ben-tic-reader + ben-local-api (bi-mode standard + migration DB auto)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"

log "✓ update OK — wired bi-mode (auto-détection histo/standard, stockage index générique, tic_mode)"
