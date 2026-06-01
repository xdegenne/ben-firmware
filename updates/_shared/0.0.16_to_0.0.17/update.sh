#!/usr/bin/env bash
# update.sh — pi-0.0.16 → pi-0.0.17
#
# Installe ben-led-release.service : un oneshot qui libère les pins LED RGB
# (12/13/16) du firmware au boot, avant que les services BEN (check_network,
# tic-reader, lora-receiver, ble-provisioner) tentent leur PWM setup.
#
# Sans ça, check_network rate son setup avec "GPIO not allocated" (le pin 13
# est tenu par le `gpio=13=op,dh` du config.txt → boot indicator firmware).
# On garde ce boot indicator (LED verte avant l'OS) — ben-led-release prend
# juste le relais dès que le système est levé.

set -euo pipefail

TRANSITION="pi-0.0.16 → pi-0.0.17"
LOG_TAG="[update $TRANSITION]"
log()  { echo "$LOG_TAG $*"; }
fail() { echo "$LOG_TAG ✗ ERREUR : $*" >&2; exit 1; }

REPO_PATH="${REPO_PATH:-/opt/ben/repo}"

# Préflight
log "préflight : pinctrl présent (nécessaire pour libérer les pins)"
command -v pinctrl >/dev/null \
    || fail "/usr/bin/pinctrl absent — apt install raspi-utils manquant ?"

log "préflight : unit source présente"
SRC="$REPO_PATH/config/systemd/ben-led-release.service"
[ -f "$SRC" ] || fail "fichier manquant : $SRC"

# Installation
log "[1/3] install /etc/systemd/system/ben-led-release.service"
sudo install -m 644 -o root -g root "$SRC" /etc/systemd/system/ben-led-release.service \
    || fail "install de l'unit a échoué"

log "[2/3] systemctl daemon-reload + enable ben-led-release"
sudo systemctl daemon-reload \
    || fail "daemon-reload a échoué"
sudo systemctl enable ben-led-release.service \
    || fail "enable ben-led-release.service a échoué"

# Application immédiate (sans reboot) — release des pins maintenant.
# Pas de fail-fast ici : si pinctrl rate (pin already free, etc.), on continue.
log "[3/3] release immédiate des pins 12/13/16 (effet sans reboot)"
sudo pinctrl set 12 ip || log "       ⚠ pinctrl 12 (non bloquant)"
sudo pinctrl set 13 ip || log "       ⚠ pinctrl 13 (non bloquant)"
sudo pinctrl set 16 ip || log "       ⚠ pinctrl 16 (non bloquant)"

log "✓ update OK — ben-led-release.service installé et enabled."
log "  Effet immédiat : la LED boot indicator est éteinte. Au prochain reboot"
log "  le service tournera automatiquement avant les autres."
