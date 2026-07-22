#!/usr/bin/env bash
# update.sh — → pi-0.9.2   : ACK APPLICATIF crypto-vérifié des trames boot (façade ben-radio).
#
# Anti cross-talk multi-logement : la façade ne renvoie l'ACK (HMAC(K_mac,nonce)) d'une trame boot
# QUE si le MAC montant est valide → l'émetteur (arduino ≥ 0.1.3) ne s'enregistre QUE chez SA
# centrale (le link-ACK RadioHead seul, émis par toute centrale en 0x20, ne suffit plus).
# Détail : ../../CHANGELOG.md (0.9.2).
#
# Changement PUREMENT code (ben_radio.py) : aucune dépendance, aucune migration, aucun changement
# d'unit → un simple restart de ben-radio. Rétro-compatible (émetteur < 0.1.3 non impacté).
# ⚠️ Ordre de déploiement : central 0.9.2 AVANT reflash d'un émetteur en 0.1.3.
# Concerne les devices `lora-tic-receiver`. Sur wired pur → skip.
# Code déjà sur disque après `git checkout pi-0.9.2`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.2"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Gate modèle : uniquement les devices qui reçoivent du LoRa TIC (façade radio) ─────────────
if ! python3 "$REPO/src/pi/capabilities.py" has lora-tic-receiver; then
    log "pas de capability lora-tic-receiver (wired pur) → façade radio non concernée (skip)"
    exit 0
fi

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
[ -f "$REPO/src/pi/ben-radio/ben_radio.py" ] || fail "manquant : ben-radio/ben_radio.py"
grep -q 'def send_app_ack' "$REPO/src/pi/ben-radio/ben_radio.py" \
    || fail "send_app_ack absent de ben_radio.py (checkout pi-0.9.2 incomplet ?)"

# ── Restart de la façade radio (ben-telemetry inchangé → pas touché) ───────────────────────────
log "[1/1] restart ben-radio (ACK applicatif crypto)"
sudo systemctl restart ben-radio.service || fail "restart ben-radio"
sleep 3
systemctl is-active ben-radio.service >/dev/null || fail "ben-radio inactif après restart"

log "✓ ben-radio actif · ACK applicatif crypto armé"
log "✓ update OK"
