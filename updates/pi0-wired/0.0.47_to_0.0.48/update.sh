#!/usr/bin/env bash
# update.sh — pi-0.0.47 → pi-0.0.48   (pi0-wired)
#
# Chantier ISOUSC standard : PREF (puissance de référence, kVA) → calibrage de la jauge en
# mode STANDARD (le standard ne fournit pas ISOUSC en A mais PREF en kVA).
#   - db.py        : colonne level_profile.pref (ALTER conditionnel, idempotent) + record_pref/get_pref
#   - main_uart.py : record_pref on-change (PREF déjà parsé en standard)
#   - local_api.py : /live arbitre maxVa = standard ? pref×1000 : isousc×230 (+ expose pref)
#
# La migration ALTER tourne au 1er db.connect du reader (idempotente, NON bloquante).
# AUCUNE perte de données. Code déjà sur disque après `git checkout pi-0.0.48`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.47 → pi-0.0.48"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a-t-il bien amené le code PREF ?
grep -q 'def record_pref' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (record_pref absent — checkout incomplet ?)"

log "[1/1] restart ben-tic-reader + ben-local-api (migration pref + arbitrage maxVa)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"

log "✓ update OK — calibrage jauge standard (PREF→maxVa) opérationnel"
