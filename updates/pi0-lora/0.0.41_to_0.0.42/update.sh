#!/usr/bin/env bash
# update.sh — pi-0.0.41 → pi-0.0.42   (pi0-lora)
#
# Chantier ISOUSC : le récepteur lit l'intensité souscrite (abonnement) et la
# stocke par PDL (level_profile.isousc, write-on-change). En LoRa, ISOUSC arrive
# dans la trame d'identité v0x01 (octet 13 ; émetteur Arduino ≥ 0.0.3).
# /live expose désormais `isousc` + `maxVa` (=ISOUSC×230) pour les réglages app
# et l'étalonnage de la jauge.
#
# Migration BDD = ALTER conditionnel (colonne level_profile.isousc), exécuté
# automatiquement au prochain db.connect() (au démarrage des services).
# Idempotent. Il suffit de RECHARGER le code → restart des deux services. Code
# déjà sur disque après `git checkout pi-0.0.42`. Tourne en `ben`.

set -euo pipefail

TR="pi-0.0.41 → pi-0.0.42"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a bien amené le nouveau code ?
grep -q "def record_isousc" "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (lecture ISOUSC + /live maxVa)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — ISOUSC : récepteur lit/stocke l'abonnement, /live expose maxVa"
