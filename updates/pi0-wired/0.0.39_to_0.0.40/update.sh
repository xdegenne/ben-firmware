#!/usr/bin/env bash
# update.sh — pi-0.0.39 → pi-0.0.40   (pi-wired uniquement)
#
# Courbe temps réel :
#  - tic-reader/main_uart.py : lecture TIC AU FIL DE L'EAU (plus de sleep 15s,
#    event-driven sur la trame) + écritures BDD BATCHÉES (1 commit/15s).
#  - store/db.py             : record_measurements_batch + curve_buckets
#    (bucketing ABSOLU + quantifié, centroïde temporel, index porté).
#  - store/local_api.py      : /curve (agrégé), /measurements degrade-safe,
#    /pdls first_ts.
#
# AUCUN changement de schéma → AUCUNE migration de données. Il faut juste
# RECHARGER le code dans les process en cours → restart des deux services.
# Code déjà sur disque après `git checkout pi-0.0.40`. Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.39 → pi-0.0.40"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a bien amené le nouveau code ?
grep -q "BATCH_MAX_AGE_S" "$REPO/src/pi/tic-reader/main_uart.py" \
    || fail "main_uart.py pas à jour (checkout incomplet ?)"
grep -q "def curve_buckets" "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-tic-reader (lecture fil de l'eau + /curve)"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"

log "✓ update OK — courbe temps réel : lecture au fil de l'eau, batch BDD, /curve agrégé"
