#!/usr/bin/env bash
# update.sh — pi-0.3.0 → pi-0.4.0   (pi0-lora)
#
# Jauge bidirectionnelle CALÉE SUR L'OBSERVÉ + garde-fou raw.
#  - High-water mark d'INJECTION (level_profile.papp_inject_max_alltime) par PDL, symétrique du
#    plafond conso : standard = -papp net (MESURÉ), histo = 230×IINST (ESTIMÉ, papp plancher à 0).
#    Mis à jour AU FIL DE L'EAU (record_measurement / _batch) + BACKFILL one-shot À LA MIGRATION
#    (reconstruit depuis ~3 mois de `measurements` — une requête, gardée par l'existence de la
#    colonne → jamais rejouée) : la jauge est calibrée immédiatement, sans attendre l'accumulation.
#  - /live expose `plafond` + `injectMax` → l'app cale CHAQUE CÔTÉ de la jauge sur son propre max
#    observé (échelle linéaire -injectMax → +plafond, 0 à sa vraie place).
#  - /chart?raw=1 BORNÉ à 24 h (garde-fou perf : pas de scan brut multi-jours qui écroulerait le Pi).
# ADD COLUMN idempotent, aucune migration destructive. Code déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.3.0 → pi-0.4.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'papp_inject_max_alltime' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (HWM injection absent — checkout incomplet ?)"
grep -q 'injectMax' "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (injectMax absent — checkout incomplet ?)"
log "restart ben-lora-receiver + ben-local-api (migration HWM injection + backfill + /live)"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
log "✓ update OK — jauge observée (plafond/injectMax) + garde-fou raw 24 h"
