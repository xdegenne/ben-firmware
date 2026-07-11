#!/usr/bin/env bash
# update.sh — pi-0.6.1 → pi-0.7.0   (MONO-FLUX capabilities) : durcissement watchdog LoRa
#
# 1re transition du nouveau flux `updates_caps` (capability-aware). Ne touche QUE les devices
# ayant la capability `lora` (radio SX127x) : réinstalle l'unit ben-lora-receiver DURCIE
# (détection Oops noyau via /proc/sys/kernel/tainted + escalade StartLimitAction=reboot) et
# redémarre le receiver. Les devices SANS `lora` (wired) SKIPPENT → zéro no-op : c'est
# précisément l'apport du modèle capabilities (fini le bump per-model inutile).
set -euo pipefail
TR="pi-0.6.1 → pi-0.7.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

if python3 "$REPO/src/pi/capabilities.py" has lora; then
    log "capability lora présente → install unit durcie + restart ben-lora-receiver"
    grep -q 'kernel_died' "$REPO/src/pi/lora-receiver/main.py" \
        || fail "main.py pas à jour (détection Oops absente — checkout incomplet ?)"
    grep -q 'StartLimitAction=reboot' "$REPO/config/systemd/ben-lora-receiver.service" \
        || fail "unit pas à jour (escalade reboot absente — checkout incomplet ?)"
    sudo install -m644 -o root -g root "$REPO/config/systemd/ben-lora-receiver.service" \
         /etc/systemd/system/ben-lora-receiver.service || fail "install unit"
    sudo systemctl daemon-reload || fail "daemon-reload"
    sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
    log "✓ watchdog durci (détection Oops noyau + escalade reboot)"
else
    log "pas de capability lora (wired) → rien à faire (skip)"
fi
log "✓ update OK"
