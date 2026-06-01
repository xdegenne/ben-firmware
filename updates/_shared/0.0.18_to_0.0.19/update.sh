#!/usr/bin/env bash
# update.sh — pi-0.0.18 → pi-0.0.19
#
# Fix ben-led-release.service : retire Before=basic.target qui créait un
# ordering cycle (systemd supprimait silencieusement le job au boot).
#
# Réinstalle l'unit + daemon-reload + reboot pour que le service tourne
# enfin au prochain boot.

set -euo pipefail

TRANSITION="pi-0.0.18 → pi-0.0.19"
LOG_TAG="[update $TRANSITION]"
log()  { echo "$LOG_TAG $*"; }
fail() { echo "$LOG_TAG ✗ ERREUR : $*" >&2; exit 1; }

REPO_PATH="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO_PATH/config/systemd/ben-led-release.service"
[ -f "$SRC" ] || fail "fichier manquant : $SRC"

log "[1/2] réinstall ben-led-release.service (sans Before=basic.target)"
sudo install -m 644 -o root -g root "$SRC" /etc/systemd/system/ben-led-release.service \
    || fail "install a échoué"
sudo systemctl daemon-reload \
    || fail "daemon-reload a échoué"
log "       OK"

log "[2/2] ✓ update OK — reboot dans 5s pour valider le boot order"
sleep 5
sudo systemctl reboot
