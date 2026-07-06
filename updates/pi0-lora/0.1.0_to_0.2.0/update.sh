#!/usr/bin/env bash
# update.sh — pi-0.1.0 → pi-0.2.0   (pi0-lora)
#
# ROLLUP PAR INDEX — Phases 1+2 (côté ÉCRITURE, PUREMENT ADDITIF : aucune lecture ni champ API
# ne change). Nouvelle table `curve_rollup` = résumé pré-agrégé par (tranche 2 min, tarif index_id)
# — min/max/sum/count/index_last. Alimentée AU FIL DE L'EAU à l'ingestion (record_measurement /
# record_measurements_batch) + BACKFILL progressif de l'historique (newest-first, 1 jour/pas, borné
# ~2 s, greffé sur prune(), REPRENABLE via curseur watermark persistant `rollup_state`). Idempotent.
# Prépare la perf `/curve` + les bandes HP/HC (Phase 3, prochaine release) : le rollup se remplit
# en ARRIÈRE-PLAN, invisible tant que la lecture ne l'utilise pas → zéro régression.
# Schéma créé au 1er db.connect (CREATE TABLE IF NOT EXISTS). AUCUNE migration destructive.
# Cf. docs/rollup-par-index.md. Code déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.1.0 → pi-0.2.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'def rollup_backfill_step' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (rollup_backfill_step absent — checkout incomplet ?)"
log "restart ben-lora-receiver (rollup incrémental + backfill au fil de prune) + ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
log "✓ update OK — rollup phases 1+2 (curve_rollup + alimentation + backfill), additif, invisible"
