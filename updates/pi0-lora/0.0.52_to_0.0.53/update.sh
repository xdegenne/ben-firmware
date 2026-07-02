#!/usr/bin/env bash
# update.sh — pi-0.0.52 → pi-0.0.53   (pi0-lora)
#
# MIGRATION backfill index générique legacy. db.connect() dérive index_id/index_value
# de `tariff` + base/hchc/hchp pour les vieilles lignes histo (index_value NULL, d'avant
# que le reader ne peuple la générique en pi-0.0.43). Sans ça, /consumption fait
# COALESCE(index_value,base,hchc,hchp) qui écrase HC et HP dans une seule colonne →
# registres mal calculés (sur-comptage ~+30 % mesuré sur legacy HC/HP). ONE-SHOT via
# user_version, idempotent. L'UPDATE en masse tourne UNE fois au 1er db.connect du reader
# après MAJ (~qq s ; plus sur grosse base, ex. ben-0001 ~315k lignes) → le reader démarre
# un poil plus lentement cette fois-là. Données non perdues, juste index_value peuplé.
#
# Code déjà sur disque après `git checkout pi-0.0.53`. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.52 → pi-0.0.53"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'CASE tariff' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (migration backfill absente — checkout incomplet ?)"
log "[1/1] restart ben-lora-receiver (déclenche le backfill) + ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
log "✓ update OK — backfill index legacy appliqué (one-shot)"
