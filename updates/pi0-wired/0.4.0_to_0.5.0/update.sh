#!/usr/bin/env bash
# update.sh — pi-0.4.0 → pi-0.5.0   (pi0-wired)
#
# Alignement no-op avec pi0-lora 0.5.0 (watchdog self-test radio du récepteur LoRa). Le wired ne fait
# PAS tourner ben-lora-receiver (lecture TIC directe) → l'unit modifiée est réinstallée mais reste
# INERTE. Bump pour garder les deux modèles + INITIAL_TAG alignés. AUCUNE migration. Tourne en `ben`.
set -euo pipefail
TR="pi-0.4.0 → pi-0.5.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'WatchdogSec' "$REPO/config/systemd/ben-lora-receiver.service" \
    || fail "unit pas à jour (checkout incomplet ?)"
log "install unit ben-lora-receiver.service (inerte en wired) + daemon-reload"
sudo install -m 644 -o root -g root "$REPO/config/systemd/ben-lora-receiver.service" \
     /etc/systemd/system/ben-lora-receiver.service || fail "install unit"
sudo systemctl daemon-reload || fail "daemon-reload"
log "✓ update OK — alignement no-op (ben-lora-receiver inactif en wired)"
