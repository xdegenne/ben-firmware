#!/usr/bin/env bash
# update.sh — pi-0.0.47 → pi-0.0.48   (pi0-lora)
#
# Chantier ISOUSC standard : PREF (puissance de référence, kVA) → calibrage de la jauge en
# mode STANDARD (le standard ne fournit pas ISOUSC en A mais PREF en kVA).
#   - db.py             : colonne level_profile.pref (ALTER conditionnel, idempotent) + record_pref/get_pref
#   - lora-receiver/main.py : lit l'octet 14 de la trame boot v0x01 → record_pref (émetteur ≥ arduino 0.0.7)
#   - local_api.py      : /live arbitre maxVa = standard ? pref×1000 : isousc×230 (+ expose pref)
#
# La migration ALTER tourne au 1er db.connect du reader (idempotente, NON bloquante).
# Rétro-compat : émetteur < 0.0.7 → octet 14 = 0 → pref ignoré (no-op). AUCUNE perte de données.
# Code déjà sur disque après `git checkout pi-0.0.48`. Tourne en `ben`.
#
# ⚠️ Pour la jauge LoRa en standard, il faut AUSSI flasher l'Arduino en 0.0.7 (envoi du PREF octet 14).

set -euo pipefail

TR="pi-0.0.47 → pi-0.0.48"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a-t-il bien amené le code PREF ?
grep -q 'def record_pref' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (record_pref absent — checkout incomplet ?)"

log "[1/1] restart ben-local-api + ben-lora-receiver (migration pref + lecture octet 14 + arbitrage maxVa)"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — calibrage jauge standard (PREF→maxVa) opérationnel (Arduino 0.0.7 requis pour le LoRa)"
