#!/usr/bin/env bash
# update.sh — pi-0.0.48 → pi-0.0.49   (pi0-wired)
#
# Chantier index énergie bi-mode, Lot B (exposition API). Idem pi0-lora : db.py expose
# l'index GÉNÉRIQUE (index_id, index_value, src_standard, inject_total) + helper producer()
# dans /live, /measurements et curve_buckets, et remplace idx_meas_pdl_ts par l'index
# COUVRANT idx_meas_pdl_ts_papp (pdl_index, ts, papp) → l'agrégation de /curve se résout
# depuis l'index (point chaud du 7j). local_api.py sert les mêmes champs génériques.
#
# MIGRATION : index couvrant créé + ancien droppé au 1er db.connect du reader (dans _SCHEMA,
# idempotent) ; quelques secondes au démarrage sur grosse base, one-shot, NON destructif,
# AUCUNE perte de données. Exposition API purement ADDITIVE (app pas-à-jour intacte). Code
# déjà sur disque après `git checkout pi-0.0.49`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.48 → pi-0.0.49"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a-t-il bien amené le code Lot B ?
grep -q 'def producer' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (producer absent — checkout incomplet ?)"

log "[1/1] restart ben-tic-reader + ben-local-api (exposition index générique + index couvrant)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"

log "✓ update OK — index générique exposé (coût en standard) + /curve sur index couvrant"
