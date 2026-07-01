#!/usr/bin/env bash
# update.sh — pi-0.0.49 → pi-0.0.50   (pi0-lora)
#
# Chantier index énergie bi-mode, Lot B (suite) : endpoint /consumption. Le
# carry-forward (conso PAR REGISTRE sur une plage) est calculé SERVER-SIDE — Pi
# maintenant, cloud plus tard, MÊME contrat → l'app est agnostique du backend, et
# la logique n'est pas dupliquée côté client.
#   - db.py        : consumption(pdl, since, until) → {by_register:[{src_standard,
#                    index_id, wh}], total_wh}. Par registre MAX(index)-MIN(index)
#                    (index monotone → exact, immunisé au saut de registre ;
#                    COALESCE index_value/base/hchc/hchp → bi-mode + legacy histo).
#   - local_api.py : GET /consumption?pdl_index&since&until.
#
# AUCUNE migration (lecture seule sur colonnes existantes). Endpoint ADDITIF (app
# pas-à-jour intacte). Code déjà sur disque après `git checkout pi-0.0.50`. Tourne
# en `ben`.

set -euo pipefail

TR="pi-0.0.49 → pi-0.0.50"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a-t-il bien amené /consumption ?
grep -q 'def consumption' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (consumption absent — checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (endpoint /consumption)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — /consumption (conso par registre, carry-forward server-side)"
