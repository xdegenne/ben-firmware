#!/usr/bin/env bash
# update.sh — pi-0.0.48 → pi-0.0.49   (pi0-lora)
#
# Chantier index énergie bi-mode, Lot B (exposition API). Le coût/conso manquait en mode
# STANDARD : base/hchc/hchp y sont NULL, l'index vit uniquement dans index_value.
#   - db.py        : /live, /measurements et curve_buckets exposent l'index GÉNÉRIQUE
#                    (index_id, index_value, src_standard, inject_total) + helper producer()
#                    (injection constatée → jauge bidir app). Index COUVRANT
#                    idx_meas_pdl_ts_papp (pdl_index, ts, papp) remplace idx_meas_pdl_ts →
#                    l'agrégation de /curve se résout depuis l'index (point chaud du 7j).
#   - local_api.py : /live expose index_id/index_value/src_standard/inject_total + producer ;
#                    /measurements et /curve portent les mêmes champs génériques.
#
# MIGRATION : création de l'index couvrant + DROP de l'ancien, au 1er db.connect du reader
# (dans _SCHEMA, idempotent — CREATE/DROP INDEX IF [NOT] EXISTS). Sur une grosse base
# (~1 M lignes) le CREATE INDEX prend quelques secondes au démarrage du reader (one-shot,
# NON destructif, AUCUNE perte de données). Exposition API purement ADDITIVE (app pas-à-jour
# intacte). Code déjà sur disque après `git checkout pi-0.0.49`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.48 → pi-0.0.49"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a-t-il bien amené le code Lot B ?
grep -q 'def producer' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (producer absent — checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (exposition index générique + index couvrant)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — index générique exposé (coût en standard) + /curve sur index couvrant"
