#!/usr/bin/env bash
# update.sh — pi-0.0.13 → pi-0.0.14
#
# Déploie le mode provisioning BLE + boot-time network check.
# Compatible pi0-wired ET pi0-lora — un seul script partagé via symlink.
#
# IDEMPOTENT : peut être rejoué autant de fois que nécessaire sans dommage.
# AUCUNE perte de config ou de données.
# En cas d'échec, exit non-zéro avec message clair → l'OTA marque le device
# comme "stuck à 0.0.13", le système reste fonctionnel sur l'ancien code.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TRANSITION="pi-0.0.13 → pi-0.0.14"
LOG_TAG="[update $TRANSITION]"
APT_LOG="/var/log/ben-update-0.0.14-apt.log"

log()  { echo "$LOG_TAG $*"; }
warn() { echo "$LOG_TAG ⚠ $*" >&2; }
fail() { echo "$LOG_TAG ✗ ERREUR : $*" >&2; exit 1; }

REPO_PATH="${REPO_PATH:-/opt/ben/repo}"

# ---------------------------------------------------------------------------
# Préflight — on bloque tôt si quelque chose va manifestement foirer plus tard
# ---------------------------------------------------------------------------
log "préflight : connectivité Internet"
if ! ping -c 1 -W 5 1.1.1.1 >/dev/null 2>&1; then
    fail "Internet injoignable — apt/pip échoueront. Abort sans modifications."
fi

log "préflight : NetworkManager actif (nécessaire pour le mode BLE)"
systemctl is-active --quiet NetworkManager.service \
    || fail "NetworkManager.service inactif — abort."

log "préflight : repo cohérent (fichiers attendus présents)"
for f in \
    "$REPO_PATH/src/pi/provisioner/main.py" \
    "$REPO_PATH/src/pi/provisioner/check_network.py" \
    "$REPO_PATH/src/pi/provisioner/led.py" \
    "$REPO_PATH/src/pi/provisioner/wifi_config.py" \
    "$REPO_PATH/src/pi/provisioner/requirements.txt" \
    "$REPO_PATH/config/systemd/ben-ble-provisioner.service" \
    "$REPO_PATH/config/systemd/ben-network-check.service" \
; do
    [ -f "$f" ] || fail "fichier manquant après git checkout : $f"
done

# Détection du modèle pour log informatif (la logique reste la même).
MODEL="unknown"
if [ -f /etc/ben-firmware/device.json ]; then
    MODEL=$(python3 -c '
import json
try:
    print(json.load(open("/etc/ben-firmware/device.json"))["model"])
except Exception:
    print("unknown")
' 2>/dev/null || echo "unknown")
fi
log "modèle détecté : $MODEL"

# ---------------------------------------------------------------------------
# 1. Dépendances système (apt — idempotent : skip si déjà installé)
# ---------------------------------------------------------------------------
log "[1/5] apt install python3-dbus python3-gi"
sudo apt-get install -y --no-install-recommends python3-dbus python3-gi \
    >"$APT_LOG" 2>&1 \
    || fail "apt install a échoué — voir $APT_LOG"
log "       OK"

# ---------------------------------------------------------------------------
# 2. Dépendance Python (pip — bluezero)
# ---------------------------------------------------------------------------
log "[2/5] pip install bluezero"
sudo pip3 install --break-system-packages --quiet bluezero \
    || fail "pip install bluezero a échoué"
# Validation forte : on importe vraiment la lib
sudo python3 -c "import bluezero, bluezero.peripheral, bluezero.adapter" \
    || fail "import bluezero échoue après installation"
log "       OK"

# ---------------------------------------------------------------------------
# 3. Units systemd (copie avec backup de l'éventuel ancien)
# ---------------------------------------------------------------------------
log "[3/5] installation des units systemd"
for unit in ben-ble-provisioner.service ben-network-check.service; do
    src="$REPO_PATH/config/systemd/$unit"
    dst="/etc/systemd/system/$unit"
    if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
        sudo cp -p "$dst" "$dst.bak-0.0.13"
        log "       backup créé : $dst.bak-0.0.13"
    fi
    sudo install -m 644 -o root -g root "$src" "$dst"
    log "       installé    : $dst"
done

# ---------------------------------------------------------------------------
# 4. systemd : daemon-reload + enable du décideur boot-time
# ---------------------------------------------------------------------------
log "[4/5] systemctl daemon-reload + enable ben-network-check"
sudo systemctl daemon-reload \
    || fail "daemon-reload a échoué"
sudo systemctl enable ben-network-check.service \
    || fail "enable ben-network-check.service a échoué"
log "       OK"

# ---------------------------------------------------------------------------
# 5. Vérifications post-install — on s'assure que tout est sain
# ---------------------------------------------------------------------------
log "[5/5] vérifications post-install"

# unit ben-network-check enabled
[ "$(systemctl is-enabled ben-network-check.service 2>&1)" = "enabled" ] \
    || fail "ben-network-check.service n'est pas enabled"

# unit ben-ble-provisioner chargeable (mais static — pas WantedBy)
ble_state=$(systemctl show -p LoadState --value ben-ble-provisioner.service)
[ "$ble_state" = "loaded" ] \
    || fail "ben-ble-provisioner.service non chargée (LoadState=$ble_state)"

# tic-reader / lora-receiver restent enabled (selon modèle) — on ne les a pas touchés
case "$MODEL" in
    pi0-wired)
        systemctl is-enabled --quiet ben-tic-reader.service \
            || warn "ben-tic-reader.service n'est plus enabled — à vérifier manuellement"
        ;;
    pi0-lora)
        systemctl is-enabled --quiet ben-lora-receiver.service \
            || warn "ben-lora-receiver.service n'est plus enabled — à vérifier manuellement"
        ;;
esac

log "✓ update OK — mode BLE provisioning installé et armé."
log "  Au prochain boot, ben-network-check ping 1.1.1.1 → décide :"
log "    Internet OK → tic-reader/lora-receiver normal"
log "    Internet KO → ben-ble-provisioner démarre, LED violet/jaune"
log ""
log "Pas de reboot automatique. Le ben-update.timer ou un reboot manuel"
log "déclenchera l'effet complet."
