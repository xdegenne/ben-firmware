#!/usr/bin/env bash
# update.sh — pi-0.0.51 → pi-0.0.52   (pi0-wired)
#
# CHECKPOINT WAL périodique : db.prune() fait désormais PRAGMA wal_checkpoint(TRUNCATE)
# (~1×/h) → le fichier -wal, qui ne se tronque jamais seul, reste borné (accélère les
# lectures). Le tic-reader wired exécute prune → bénéficie du fix. Maintenance pure,
# AUCUNE donnée modifiée. (L'instrumentation "INDEX0" de pi0-lora est côté récepteur
# LoRa → sans objet en wired.)
#
# AUCUNE migration. Code déjà sur disque après `git checkout pi-0.0.52`. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.51 → pi-0.0.52"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'wal_checkpoint(TRUNCATE)' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (checkpoint WAL absent — checkout incomplet ?)"
log "[1/1] restart ben-tic-reader + ben-local-api (checkpoint WAL)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"
log "✓ update OK — WAL borné (checkpoint horaire)"
