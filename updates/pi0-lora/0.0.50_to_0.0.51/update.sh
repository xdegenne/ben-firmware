#!/usr/bin/env bash
# update.sh — pi-0.0.50 → pi-0.0.51   (pi0-lora)
#
# Robustesse aux `index_value=0` parasites (carry-forward EASF empoisonné côté
# émetteur — cause racine à confirmer, cf. chantier Arduino). Un index compteur
# cumulatif n'est JAMAIS 0 ; sinon le MAX-MIN de /consumption renvoyait l'index
# ABSOLU (~15 MWh → coût délirant, ex. 2957 €/jour). Deux gardes dans db.py :
#   - /consumption : filtre COALESCE(...) > 0 (au lieu de IS NOT NULL).
#   - écriture (_generic_cols) : index_value == 0 → NULL → donnée BRUTE propre
#     (courbe, /measurements, futur cloud), pas seulement /consumption.
#
# AUCUNE migration (garde en lecture + normalisation des NOUVELLES écritures ; les 0
# déjà stockés restent mais /consumption les ignore). Code déjà sur disque après
# `git checkout pi-0.0.51`. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.50 → pi-0.0.51"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'hchp) > 0' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (garde index_value>0 absente — checkout incomplet ?)"
log "[1/1] restart ben-local-api + ben-lora-receiver (gardes index_value=0)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
log "✓ update OK — index_value=0 parasites neutralisés (/consumption + écriture)"
