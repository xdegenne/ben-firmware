#!/usr/bin/env bash
# update.sh — pi-0.0.53 → pi-0.0.54   (pi0-wired)
#
# Chantier « unification labels + contrat », côté WIRED. main_uart lit LTARF + le contrat
# (NGTF en standard / OPTARIF en historique, mode-agnostique) directement dans la TIC et les
# stocke (tariff_labels keyé par contrat + level_profile.ngtf). Résolution de label unifiée
# serveur + API : /live.tariff_label + /live.contract + nouvel endpoint /registers.
# STRICTEMENT ADDITIF (aucun champ retiré → app legacy inchangées ; histo intact).
#
# MIGRATION (1er db.connect) : CREATE tariff_labels + ALTER level_profile.ngtf, idempotent.
# Code déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.53 → pi-0.0.54"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'def resolve_label' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (resolve_label absent — checkout incomplet ?)"
log "restart ben-tic-reader (capture LTARF/NGTF/OPTARIF + migration) + ben-local-api"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"
log "✓ update OK — labels/contrat unifiés (wired)"
