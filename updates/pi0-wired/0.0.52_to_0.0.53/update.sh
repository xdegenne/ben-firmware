#!/usr/bin/env bash
# update.sh — pi-0.0.52 → pi-0.0.53   (pi0-wired)
#
# MIGRATION backfill index générique legacy (idem pi0-lora). db.connect() dérive
# index_id/index_value de `tariff` + base/hchc/hchp pour les vieilles lignes histo
# (index_value NULL). Corrige /consumption sur le legacy HC/HP (sur-comptage ~+30 %
# sinon). ONE-SHOT via user_version, idempotent. UPDATE en masse au 1er db.connect du
# tic-reader après MAJ (~qq s ; ex. ben-0003 ~241k lignes) → démarrage un peu plus lent
# cette fois-là. Données non perdues, index_value peuplé.
#
# Code déjà sur disque après `git checkout pi-0.0.53`. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.52 → pi-0.0.53"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'CASE tariff' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (migration backfill absente — checkout incomplet ?)"
log "[1/1] restart ben-tic-reader (déclenche le backfill) + ben-local-api"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"
log "✓ update OK — backfill index legacy appliqué (one-shot)"
