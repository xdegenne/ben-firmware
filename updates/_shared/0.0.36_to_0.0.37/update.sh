#!/usr/bin/env bash
# update.sh — pi-0.0.36 → pi-0.0.37
#
# API locale : POST /unprovision corrigé.
#  - répond AVANT de couper le réseau (sinon supprimer la connexion WiFi tue le
#    lien TCP et l'app ne reçoit jamais l'ack) ;
#  - désappairage + reboot en ASYNCHRONE (Timer) ;
#  - oublie TOUTES les connexions WiFi (ben-provisioned + éventuel profil
#    opérateur du golden) → repart vraiment en provisioning BLE ;
#  - ajout de logs « [unprovision] … » (l'API silence le log par défaut).
#  → restart ben-local-api pour charger la nouvelle version.
#
# Code sur disque après `git checkout pi-0.0.37`. Pas de reboot ici. Tourne en
# `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.36 → pi-0.0.37"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "_teardown" "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (checkout incomplet ?)"

log "[1/1] restart ben-local-api (charge le /unprovision corrigé)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"

log "✓ update OK — /unprovision : ack avant coupure, reboot async, oubli WiFi complet, logs"
