#!/usr/bin/env bash
# update.sh — pi-0.0.51 → pi-0.0.52   (pi0-lora)
#
# 1) CHECKPOINT WAL périodique : db.prune() fait désormais PRAGMA wal_checkpoint(TRUNCATE)
#    (~1×/h). Le fichier -wal ne se tronque jamais seul (observé 392 Mo sur ben-0001 →
#    ralentit toutes les lectures) ; il reste maintenant borné. Maintenance pure, AUCUNE
#    donnée modifiée.
# 2) INSTRUMENTATION index=0 : le récepteur logge un WARNING greppable "INDEX0" (contexte
#    batch_seq / NTARF / rssi / snr / gap) quand l'émetteur envoie un keyframe
#    index_value=0 → capture le contexte de la cause racine (carry-forward EASF empoisonné,
#    à corréler). Que du log.
#
# AUCUNE migration. Code déjà sur disque après `git checkout pi-0.0.52`. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.51 → pi-0.0.52"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'wal_checkpoint(TRUNCATE)' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (checkpoint WAL absent — checkout incomplet ?)"
log "[1/1] restart ben-local-api + ben-lora-receiver (checkpoint WAL + log INDEX0)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
log "✓ update OK — WAL borné (checkpoint horaire) + instrumentation INDEX0"
