#!/usr/bin/env bash
# update.sh — pi-0.4.0 → pi-0.5.0   (pi0-lora)
#
# Watchdog SELF-TEST RADIO (récepteur LoRa). main.py lit périodiquement REG_VERSION du SX127x
# (0x42 doit valoir 0x12) et pingue sd_notify(WATCHDOG=1) tant que la radio répond ; si le SPI/radio
# est figé (incident ben-0001 09/07), il cesse de pinguer → systemd restart le service (WatchdogSec=90s ;
# l'état indexes/seq est PERSISTÉ → restart sûr). Le silence de trames (émetteur off) NE déclenche PAS
# de restart (le self-test passe quand même) — VALIDÉ sur ben-0001 (émetteur-off 3,5 min, NRestarts=0).
# Unit : NotifyAccess=main, WatchdogSec=90, StartLimitIntervalSec=0 (restart infini, escalade reboot
# DIFFÉRÉE). AUCUNE migration BDD. Code + unit déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.4.0 → pi-0.5.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'radio_alive' "$REPO/src/pi/lora-receiver/main.py" \
    || fail "main.py pas à jour (self-test radio absent — checkout incomplet ?)"
grep -q 'WatchdogSec' "$REPO/config/systemd/ben-lora-receiver.service" \
    || fail "unit pas à jour (WatchdogSec absent — checkout incomplet ?)"
log "install unit ben-lora-receiver.service + daemon-reload"
sudo install -m 644 -o root -g root "$REPO/config/systemd/ben-lora-receiver.service" \
     /etc/systemd/system/ben-lora-receiver.service || fail "install unit"
sudo systemctl daemon-reload || fail "daemon-reload"
log "restart ben-lora-receiver (watchdog self-test radio actif)"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
log "✓ update OK — watchdog self-test radio (WatchdogSec=90s, restart-sur-radio-figée)"
