#!/usr/bin/env bash
# update.sh — pi-0.8.7 → pi-0.9.0   (MONO-FLUX)
#
# FAÇADE RADIO. Découpe le monolithe `ben-lora-receiver` en DEUX services reliés par un bus MQTT
# LOCAL — `ben-radio` (seul maître du RFM95 : RX/TX + LED + watchdogs) et `ben-telemetry` (décode /
# vérifie MAC / déchiffre / stocke, SANS radio ni clé). Résout le SPI-freeze / Oops noyau qu'on
# provoquait en stoppant le récepteur pour émettre une commande. Apporte aussi le canal de commande
# DESCENDANT chiffré (secure_link) et la dérivation de clé PAR-DEVICE (registre devices.yaml).
# Détails : docs/canal-commande-lora-descendant.md.
#
# NOUVELLE dépendance : un broker MQTT LOCAL (mosquitto sur 127.0.0.1, persistence OFF → zéro
# écriture SD). Installé ici si absent.
#
# Concerne les devices `lora-tic-receiver`. Sur wired pur → skip (rien à faire).
# Code déjà sur disque après `git checkout pi-0.9.0`. Tourne en `ben` + sudo. Idempotent.

set -euo pipefail
TR="pi-0.8.7 → pi-0.9.0"
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

# ── 1. Broker MQTT local (mosquitto) — install idempotent + conf + (re)start ──
log "[1/6] mosquitto (broker MQTT local, persistence off)"
if ! command -v mosquitto >/dev/null 2>&1; then
    log "  install mosquitto (apt)"
    sudo apt-get update -qq   || fail "apt-get update"
    sudo apt-get install -y mosquitto || fail "apt-get install mosquitto"
fi
sudo install -m 644 -o root -g root "$REPO/config/mosquitto/ben.conf" \
    /etc/mosquitto/conf.d/ben.conf || fail "install ben.conf"
sudo systemctl enable mosquitto || fail "enable mosquitto"
sudo systemctl restart mosquitto || fail "restart mosquitto"   # applique ben.conf (127.0.0.1, persistence off)

# ── 2. Répertoire d'état persisté de la radio (compteurs TX, backoff silence) ─
log "[2/6] /var/lib/ben-firmware (état radio persisté)"
sudo install -d -m 755 -o ben -g ben /var/lib/ben-firmware || fail "mkdir /var/lib/ben-firmware"

# ── 3. Registre devices (adresse LoRa → nom) — seed minimal si ABSENT ─────────
# Donnée de PROVISIONING : ne JAMAIS écraser un registre existant (ex. ben-0001 déclare son actuator).
log "[3/6] /etc/ben-firmware/devices.yaml (registre adresse→nom)"
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

# ── 5. SÉCURITÉ : neutraliser TEMPORAIREMENT l'escalade reboot de ben-radio ───
# Le cutover (stop monolithe → start ben-radio) est le moment À RISQUE : si l'init radio échoue
# transitoirement (GPIO/SPI pas encore relâché par le monolithe qu'on vient de stopper), l'escalade
# « 3 crashs en 300 s → reboot » pourrait REBOOTER le device EN PLEIN OTA. On la neutralise le temps
# du cutover, on la RESTAURE (§7) une fois la façade confirmée saine. En cas d'ÉCHEC, le drop-in
# RESTE volontairement → pas de reboot-loop sur une façade cassée, device joignable/diagnosticable.
log "[5/7] neutralisation temporaire de l'escalade reboot (le temps du cutover)"
sudo mkdir -p /etc/systemd/system/ben-radio.service.d
printf '[Unit]\n# Posé par update.sh 0.9.0 le temps du cutover, retiré après vérif (§7).\nStartLimitIntervalSec=0\n' \
    | sudo tee /etc/systemd/system/ben-radio.service.d/ota-cutover-no-reboot.conf >/dev/null
sudo systemctl daemon-reload || fail "daemon-reload (drop-in no-reboot)"

# ── 6. Cutover runtime : stop monolithe → start façade + vérif ────────────────
# Le câblage BOOT a déjà basculé dans capabilities.py (checkout) → prochain boot = façade.
# Le monolithe reste INSTALLÉ (fallback) mais n'est plus ni démarré ni câblé. Idempotent.
log "[6/7] cutover : stop ben-lora-receiver → start ben-radio + ben-telemetry"
sudo systemctl stop ben-lora-receiver.service 2>/dev/null || true
sudo systemctl restart ben-radio.service     || fail "start ben-radio"
sudo systemctl restart ben-telemetry.service || fail "start ben-telemetry"
sleep 3
systemctl is-active ben-radio.service     >/dev/null || fail "ben-radio inactif après start"
systemctl is-active ben-telemetry.service >/dev/null || fail "ben-telemetry inactif après start"

# ── 7. Façade CONFIRMÉE saine → RESTAURER l'escalade reboot (retrait du drop-in) ─
log "[7/7] restauration de l'escalade reboot (façade saine)"
sudo rm -f /etc/systemd/system/ben-radio.service.d/ota-cutover-no-reboot.conf
sudo rmdir /etc/systemd/system/ben-radio.service.d 2>/dev/null || true
sudo systemctl daemon-reload || fail "daemon-reload (restore escalade)"

log "✓ façade active (ben-radio + ben-telemetry) · monolithe en fallback · mosquitto local · escalade reboot armée"
log "✓ update OK"
