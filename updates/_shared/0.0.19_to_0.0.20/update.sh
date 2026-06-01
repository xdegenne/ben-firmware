#!/usr/bin/env bash
# update.sh — pi-0.0.19 → pi-0.0.20
#
# 1) Restaure gpio=13=op,dh dans config.txt si absent (pi-0.0.17 buggy
#    l'avait stripé sur ben-0001 et ben-0003 — boot indicator manquant).
# 2) Réinstall ben-led-release.service qui fait désormais une séquence
#    "welcome" : kill boot indicator → flash blanc 150ms → off → release.
#
# Reboot final pour relecture config.txt par le firmware (étape 1)
# ET pour valider que la séquence welcome marche au boot.

set -euo pipefail

TRANSITION="pi-0.0.19 → pi-0.0.20"
LOG_TAG="[update $TRANSITION]"
log()  { echo "$LOG_TAG $*"; }
fail() { echo "$LOG_TAG ✗ ERREUR : $*" >&2; exit 1; }

REPO_PATH="${REPO_PATH:-/opt/ben/repo}"

# 1. Restaure gpio=13=op,dh dans config.txt
if [ -f /boot/firmware/config.txt ]; then
    CONFIG_TXT="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
    CONFIG_TXT="/boot/config.txt"
else
    fail "config.txt introuvable"
fi

log "[1/2] vérification gpio=13=op,dh dans $CONFIG_TXT"
if ! grep -q "^gpio=13=op,dh$" "$CONFIG_TXT"; then
    log "       absent → restauration"
    sudo cp -p "$CONFIG_TXT" "$CONFIG_TXT.bak-pre-0.0.20"
    echo "gpio=13=op,dh" | sudo tee -a "$CONFIG_TXT" >/dev/null \
        || fail "append a échoué"
    log "       ✓ restauré"
else
    log "       déjà présent"
fi

# 2. Réinstall ben-led-release.service (nouvelle séquence welcome flash)
SRC="$REPO_PATH/config/systemd/ben-led-release.service"
[ -f "$SRC" ] || fail "fichier manquant : $SRC"

log "[2/2] réinstall ben-led-release.service (nouvelle séquence welcome)"
sudo install -m 644 -o root -g root "$SRC" /etc/systemd/system/ben-led-release.service \
    || fail "install a échoué"
sudo systemctl daemon-reload \
    || fail "daemon-reload a échoué"
log "       ✓ OK"

log "✓ update OK — reboot dans 5s (relecture config.txt + test séquence)"
sleep 5
sudo systemctl reboot
