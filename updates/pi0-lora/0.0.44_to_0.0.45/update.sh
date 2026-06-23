#!/usr/bin/env bash
# update.sh — pi-0.0.44 → pi-0.0.45   (pi0-lora)
#
# Unpair « supprimer les données » (POST /unprovision?wipe=1) :
#   - on STOPPE le reader avant le rm de la base → wipe propre (base plus ouverte
#     en WAL, pas d'écriture dans un inode supprimé) ;
#   - avec wipe on ÉTEINT le boîtier (poweroff) au lieu de reboot → prépa livraison
#     béta (part hors tension, le testeur le rallume pour provisionner via l'app).
#   - unpair simple (sans suppression) → reboot (re-pairing BLE) inchangé.
# Modif `local_api.py` uniquement. AUCUNE migration BDD. Code déjà sur disque après
# `git checkout pi-0.0.45`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.44 → pi-0.0.45"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "poweroff (wipe)" "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (unprovision poweroff absent — checkout incomplet ?)"

log "[1/1] restart ben-local-api (unprovision wipe → stop reader + poweroff)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"

log "✓ update OK — unpair+wipe : stop reader + poweroff (prépa livraison)"
