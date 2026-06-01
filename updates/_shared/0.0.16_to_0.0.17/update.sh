#!/usr/bin/env bash
# update.sh — pi-0.0.16 → pi-0.0.17
#
# Strip les directives `gpio=*=op,dh` du firmware Pi (config.txt) qui tenaient
# les pins LED RGB au boot et empêchaient ben-network-check.service de faire
# son setup PWM ("GPIO not allocated" / "GPIO busy").
#
# Effet : la LED reste éteinte pendant ~30s de boot puis les services BEN
# prennent la main proprement (LED bleue check_network, etc.).
#
# Une reboot est nécessaire pour que config.txt soit relu par le firmware.

set -euo pipefail

TRANSITION="pi-0.0.16 → pi-0.0.17"
LOG_TAG="[update $TRANSITION]"
log()  { echo "$LOG_TAG $*"; }
fail() { echo "$LOG_TAG ✗ ERREUR : $*" >&2; exit 1; }

# Pi OS Bookworm/Trixie : /boot/firmware/config.txt. Fallback /boot/config.txt.
if [ -f /boot/firmware/config.txt ]; then
    CONFIG_TXT="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
    CONFIG_TXT="/boot/config.txt"
else
    fail "config.txt introuvable (ni /boot/firmware/ ni /boot/)"
fi
log "config.txt : $CONFIG_TXT"

# Backup défensif
sudo cp -p "$CONFIG_TXT" "$CONFIG_TXT.bak-0.0.16" \
    || fail "backup de $CONFIG_TXT a échoué"
log "backup : $CONFIG_TXT.bak-0.0.16"

# Strip GPIO 12 / 13 / 16 boot indicators
sudo sed -i '/^gpio=12=op,dh$/d; /^gpio=13=op,dh$/d; /^gpio=16=op,dh$/d' "$CONFIG_TXT" \
    || fail "sed sur $CONFIG_TXT a échoué"

# Vérification : aucune ligne restante
if grep -E '^gpio=(12|13|16)=op,dh$' "$CONFIG_TXT" >/dev/null; then
    fail "des directives gpio= persistent dans $CONFIG_TXT — strip incomplet"
fi
log "GPIO 12/13/16 boot indicators retirés"

# Reboot : config.txt n'est lu qu'au démarrage du firmware. Sans reboot la
# correction reste latente jusqu'à la prochaine coupure manuelle.
log "✓ update OK — reboot dans 5s pour appliquer le nouveau config.txt"
sleep 5
sudo systemctl reboot
