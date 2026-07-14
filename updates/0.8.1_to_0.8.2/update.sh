#!/usr/bin/env bash
# update.sh — pi-0.8.1 → pi-0.8.2   (MONO-FLUX) : fixes unboxing BLE (provisioner)
#
# CONTEXTE : correctifs du PROVISIONER BLE (unboxing). AUCUN impact sur un device en service —
# le provisioner n'est lancé qu'à l'unboxing (avant provisioning) :
#   - DEVICE_INFO compact : le device.json complet (capabilities) fait 186 o > MTU iOS ~185 →
#     lecture ATT tronquée → l'app iOS échouait à parser (« erreur inattendue »). Compact ~94 o.
#     (Android non affecté : MTU 512.)
#   - scan WiFi UNIQUE au démarrage (au lieu d'un rescan toutes les 30 s) : radio WiFi+BLE partagée
#     sur Pi Zero W → un rescan pendant une connexion BLE affamait le lien → décrochage (Android à
#     supervision timeout court).
#   - agent de pairing Just Works (NoInputNoOutput, in-process) pour BlueZ 5.x (Service Changed).
#
# → RIEN à redémarrer : le provisioner ne tourne pas sur un device en service. Le code est checké
#   out par le tag et servira au PROCHAIN provisioning / re-provisioning. No-op volontaire.
set -euo pipefail
TR="pi-0.8.1 → pi-0.8.2"
log() { echo "[update $TR] $*"; }

REPO="${REPO_PATH:-/opt/ben/repo}"
# Garde-fou : vérifier que le nouveau provisioner (fix scan WiFi) est bien checké out.
grep -q '_wifi_scan_once' "$REPO/src/pi/provisioner/main.py" \
    || { echo "[update $TR] ✗ ERREUR : provisioner pas à jour (fix absent — checkout incomplet ?)" >&2; exit 1; }

log "fixes unboxing BLE (provisioner) — actifs au prochain provisioning"
log "✓ update OK"
