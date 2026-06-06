#!/usr/bin/env bash
# update.sh — pi-0.0.28 → pi-0.0.29
#
# Fix race de services au provisioning BLE.
#   - ben-tic-reader.service / ben-lora-receiver.service : SUPPRESSION de
#     [Install]/WantedBy → plus d'autostart au boot. Ils sont démarrés
#     UNIQUEMENT par check_network.py, et seulement si le réseau est up.
#   - check_network.py : démarre le bon reader selon le modèle quand online ;
#     et détecte "jamais provisionné" (pas de connexion `ben-provisioned`) →
#     bascule BLE directe sans pinguer 30s au premier boot.
#
# Avant ce fix, tic-reader (enabled + Restart=always) démarrait au boot en
# doublon et, via Conflicts, tuait ben-ble-provisioner → "code couleur affiché
# puis ré-écoute BLE en boucle", provisioning impossible (typiquement en démo
# sans Linky branché, où tic-reader crashe et se relance toutes les 10s).
#
# Migration : réinstaller les units modifiées + retirer les symlinks d'autostart.
# Le code (check_network.py) est déjà sur disque via le git checkout.
# N'impacte PAS un device en service : son reader tourne jusqu'au prochain
# reboot, où check_network le relancera proprement (réseau up).
#
# Tourne en `ben` (sudo pour les systemctl). Idempotent.

set -euo pipefail

TR="pi-0.0.28 → pi-0.0.29"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fous : le checkout doit bien porter le nouveau code.
grep -q "READERS_BY_MODEL" "$REPO/src/pi/provisioner/check_network.py" \
    || fail "check_network.py pas à jour (checkout incomplet ?)"
grep -qE '^WantedBy=' "$REPO/config/systemd/ben-tic-reader.service" \
    && fail "ben-tic-reader.service a encore un WantedBy= actif (checkout incomplet ?)"

log "[1/3] réinstallation des units systemd modifiées"
sudo install -m 644 "$REPO/config/systemd/ben-tic-reader.service" \
    /etc/systemd/system/ben-tic-reader.service
sudo install -m 644 "$REPO/config/systemd/ben-lora-receiver.service" \
    /etc/systemd/system/ben-lora-receiver.service

log "[2/3] retrait de l'autostart (readers désormais lancés par check_network)"
sudo systemctl disable ben-tic-reader.service    2>/dev/null || true
sudo systemctl disable ben-lora-receiver.service 2>/dev/null || true

log "[3/3] daemon-reload"
sudo systemctl daemon-reload

log "✓ update OK — race provisioning corrigée (effet au prochain boot)"
