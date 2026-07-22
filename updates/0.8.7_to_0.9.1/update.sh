#!/usr/bin/env bash
# update.sh — → pi-0.9.1   (MONO-FLUX)   [servi par les transitions 0.8.7→0.9.1 ET 0.9.0→0.9.1]
#
# 0.9.1 = 0.9.0 (façade radio ben-radio + ben-telemetry via bus MQTT local) + LE FIX QUI MANQUAIT :
#   install de python3-paho-mqtt (lib CLIENTE MQTT). La 0.9.0 posait bien mosquitto (le BROKER)
#   mais oubliait paho (la lib que ben-radio ET ben-telemetry importent) → au 1er `import paho`,
#   ben-radio crashe → crash-loop → escalade reboot → BOUCLE INFERNALE (incident device-neuf
#   ben-0011 + OTA ben-0003, 2026-07-22). Le tag 0.9.0 étant immuable, on corrige via 0.9.1.
#
# IDEMPOTENT et sûr depuis 0.8.7 (cutover complet) OU 0.9.0 (façade déjà là → juste paho + restart).
# Concerne les devices `lora-tic-receiver`. Sur wired pur → skip.
# Code déjà sur disque après `git checkout pi-0.9.1`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.1"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Gate modèle : uniquement les devices qui reçoivent du LoRa TIC ────────────
if ! python3 "$REPO/src/pi/capabilities.py" has lora-tic-receiver; then
    log "pas de capability lora-tic-receiver (wired pur) → façade radio non concernée (skip)"
    exit 0
fi

# ── Préflight : le code de la façade doit être présent (checkout du tag en amont) ──
[ -f "$REPO/src/pi/ben-radio/ben_radio.py" ]         || fail "manquant : ben-radio/ben_radio.py"
[ -f "$REPO/src/pi/ben-telemetry/ben_telemetry.py" ] || fail "manquant : ben-telemetry/ben_telemetry.py"
[ -f "$REPO/src/pi/lora-receiver/secure_link.py" ]   || fail "manquant : lora-receiver/secure_link.py"
[ -f "$REPO/config/systemd/ben-radio.service" ]      || fail "manquant : ben-radio.service"
[ -f "$REPO/config/systemd/ben-telemetry.service" ]  || fail "manquant : ben-telemetry.service"
[ -f "$REPO/config/mosquitto/ben.conf" ]             || fail "manquant : mosquitto/ben.conf"

# ── 1. Deps MQTT : mosquitto (BROKER) + python3-paho-mqtt (lib CLIENTE) ────────
# LE FIX 0.9.1 : paho est OBLIGATOIRE (ben-radio/ben-telemetry font `import paho.mqtt.client`).
log "[1/7] deps MQTT (mosquitto broker + python3-paho-mqtt client)"
NEED=""
command -v mosquitto >/dev/null 2>&1                  || NEED="$NEED mosquitto"
python3 -c "import paho.mqtt.client" >/dev/null 2>&1   || NEED="$NEED python3-paho-mqtt"
if [ -n "$NEED" ]; then
    log "  install :$NEED"
    sudo apt-get update -qq          || fail "apt-get update"
    sudo apt-get install -y $NEED     || fail "apt-get install$NEED"
fi
# garde-fou : sans paho, on N'ENCLENCHE PAS le cutover (sinon crash-loop garanti).
python3 -c "import paho.mqtt.client" >/dev/null 2>&1 || fail "python3-paho-mqtt toujours absent → abandon avant cutover"
sudo install -m 644 -o root -g root "$REPO/config/mosquitto/ben.conf" \
    /etc/mosquitto/conf.d/ben.conf || fail "install ben.conf"
sudo systemctl enable mosquitto || fail "enable mosquitto"
sudo systemctl restart mosquitto || fail "restart mosquitto"   # applique ben.conf (127.0.0.1, persistence off)

# ── 2. Répertoire d'état persisté de la radio (compteurs TX, backoff silence) ─
log "[2/7] /var/lib/ben-firmware (état radio persisté)"
sudo install -d -m 755 -o ben -g ben /var/lib/ben-firmware || fail "mkdir /var/lib/ben-firmware"

# ── 3. Registre devices (adresse LoRa → nom) — seed minimal si ABSENT ─────────
# Donnée de PROVISIONING : ne JAMAIS écraser un registre existant.
log "[3/7] /etc/ben-firmware/devices.yaml (registre adresse→nom)"
sudo install -d -m 755 /etc/ben-firmware
if [ ! -f /etc/ben-firmware/devices.yaml ]; then
    log "  seed registre par défaut : 0x1f: tic"
    printf '# Registre adresse LoRa (hex) -> nom device (donnee de provisioning).\n# K_device = HMAC(K_racine, nom). Ajouter un device = 1 ligne + flash (cle+adresse EEPROM).\n0x1f: tic\n' \
        | sudo tee /etc/ben-firmware/devices.yaml >/dev/null
    sudo chmod 644 /etc/ben-firmware/devices.yaml
else
    log "  registre déjà présent → conservé"
fi

# ── 4. Install des 2 units de la façade (ben-radio porte l'escalade reboot) ────
log "[4/7] install ben-radio.service + ben-telemetry.service"
sudo install -m 644 -o root -g root "$REPO/config/systemd/ben-radio.service" \
    /etc/systemd/system/ben-radio.service || fail "install ben-radio.service"
sudo install -m 644 -o root -g root "$REPO/config/systemd/ben-telemetry.service" \
    /etc/systemd/system/ben-telemetry.service || fail "install ben-telemetry.service"
sudo systemctl daemon-reload || fail "daemon-reload"

# ── 5. SÉCURITÉ : neutraliser TEMPORAIREMENT l'escalade reboot pendant le cutover ─
# Si l'init radio échoue transitoirement (GPIO/SPI pas relâché), l'escalade 3-crashs→reboot pourrait
# rebooter EN PLEIN OTA. On la neutralise, on la RESTAURE (§7) une fois la façade saine. En cas
# d'ÉCHEC, le drop-in RESTE → pas de reboot-loop sur façade cassée, device joignable/diagnosticable.
log "[5/7] neutralisation temporaire de l'escalade reboot (le temps du cutover)"
sudo mkdir -p /etc/systemd/system/ben-radio.service.d
printf '[Unit]\n# Posé par update.sh 0.9.1 le temps du cutover, retiré après vérif (§7).\nStartLimitIntervalSec=0\n' \
    | sudo tee /etc/systemd/system/ben-radio.service.d/ota-cutover-no-reboot.conf >/dev/null
sudo systemctl daemon-reload || fail "daemon-reload (drop-in no-reboot)"

# ── 6. Cutover runtime : stop monolithe → start façade + vérif ────────────────
log "[6/7] cutover : stop ben-lora-receiver → start ben-radio + ben-telemetry"
sudo systemctl stop ben-lora-receiver.service 2>/dev/null || true
sudo systemctl restart ben-radio.service     || fail "start ben-radio"
sudo systemctl restart ben-telemetry.service || fail "start ben-telemetry"
sleep 3
systemctl is-active ben-radio.service     >/dev/null || fail "ben-radio inactif après start (paho ?)"
systemctl is-active ben-telemetry.service >/dev/null || fail "ben-telemetry inactif après start (paho ?)"

# ── 7. Façade CONFIRMÉE saine → RESTAURER l'escalade reboot (retrait du drop-in) ─
log "[7/7] restauration de l'escalade reboot (façade saine)"
sudo rm -f /etc/systemd/system/ben-radio.service.d/ota-cutover-no-reboot.conf
sudo rmdir /etc/systemd/system/ben-radio.service.d 2>/dev/null || true
sudo systemctl daemon-reload || fail "daemon-reload (restore escalade)"

log "✓ façade active (ben-radio + ben-telemetry) · paho présent · mosquitto local · escalade reboot armée"
log "✓ update OK"
