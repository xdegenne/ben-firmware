#!/usr/bin/env bash
# update.sh — pi-0.0.53 → pi-0.0.54   (pi0-lora)
#
# Chantier « unification labels + contrat ». Récepteur LoRa : décode l'EXT COURBE v2 (flag
# bit7) = EAIT + LTARF (label tarif std) + DIAG index=0 ; + contrat NGTF/OPTARIF dans la trame
# boot v0x01 (octet 15 = len, 16.. = ascii). Résolution de label UNIFIÉE côté serveur
# (resolve_label : std=LTARF autoritatif / histo=convention) + API : /live.tariff_label +
# /live.contract + nouvel endpoint /registers. Stockage : tariff_labels keyé
# (pdl,src,index_id,ngtf) (segmenté par contrat) + level_profile.ngtf.
# STRICTEMENT ADDITIF (aucun champ retiré/renommé → app legacy inchangées ; wired histo intact).
#
# MIGRATION (1er db.connect) : CREATE tariff_labels (PK avec ngtf) + ALTER level_profile.ngtf,
# idempotent (drop/recreate tariff_labels si ancienne PK). Code déjà sur disque après checkout.
# Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.53 → pi-0.0.54"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'def resolve_label' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (resolve_label absent — checkout incomplet ?)"
log "restart ben-lora-receiver (décodage ext v2 + migration) + ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
log "✓ update OK — labels/contrat unifiés (LTARF ext + NGTF/OPTARIF boot + /registers)"
