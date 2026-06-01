#!/usr/bin/env bash
# update.sh — pi-0.0.17 → pi-0.0.18
#
# Installe ben-led-release.service (oneshot Before= tous les services BEN qui
# libère les pins GPIO 12/13/16 du firmware via `pinctrl set <pin> ip`).
#
# Contexte : pi-0.0.17 a publié un update.sh qui stripait `gpio=13=op,dh`
# du config.txt et rebootait. ben-0001 a appliqué — boot indicator absent
# mais système fonctionnel. On NE restaure pas le boot indicator (cosmétique).
# On installe juste le service pour propreté + on reboot pour valider que
# l'enchaînement marche bout-en-bout.
#
# Idempotent.

set -euo pipefail

TRANSITION="pi-0.0.17 → pi-0.0.18"
LOG_TAG="[update $TRANSITION]"
log()  { echo "$LOG_TAG $*"; }
fail() { echo "$LOG_TAG ✗ ERREUR : $*" >&2; exit 1; }

REPO_PATH="${REPO_PATH:-/opt/ben/repo}"

# Préflight
SRC="$REPO_PATH/config/systemd/ben-led-release.service"
[ -f "$SRC" ] || fail "fichier manquant : $SRC"
command -v pinctrl >/dev/null \
    || fail "/usr/bin/pinctrl absent (raspi-utils manquant)"

# 1. Install ben-led-release.service
log "[1/3] install ben-led-release.service"
sudo install -m 644 -o root -g root "$SRC" /etc/systemd/system/ben-led-release.service \
    || fail "install de l'unit a échoué"
sudo systemctl daemon-reload \
    || fail "daemon-reload a échoué"
sudo systemctl enable ben-led-release.service \
    || fail "enable a échoué"
log "       OK"

# 2. Release immédiate des pins (effet sans reboot)
log "[2/3] release immédiate des pins 12/13/16"
sudo pinctrl set 12 ip || log "       ⚠ pinctrl 12 (non bloquant)"
sudo pinctrl set 13 ip || log "       ⚠ pinctrl 13 (non bloquant)"
sudo pinctrl set 16 ip || log "       ⚠ pinctrl 16 (non bloquant)"
log "       OK"

# 3. Reboot pour valider l'enchaînement complet (boot → ben-led-release →
#    check_network → lora-receiver/tic-reader/ble-provisioner).
log "[3/3] ✓ update OK — reboot dans 5s"
sleep 5
sudo systemctl reboot
