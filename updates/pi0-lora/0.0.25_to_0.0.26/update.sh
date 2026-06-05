#!/usr/bin/env bash
# update.sh — pi-0.0.25 → pi-0.0.26
#
# Tarif HC/HP exposé par l'API locale.
#   - db.py : colonne `tariff` sur measurements (index tarifaire actif :
#     0=BASE, 1=HC, 2=HP, 3+=EJP/Tempo). Migration ALTER auto au prochain
#     connect écriture. record_measurement la remplit : PTEC côté wired,
#     index actif de la trame côté LoRa.
#   - local_api.py : /live et /measurements ajoutent le champ `tariff`.
#   → l'app affiche HC/HP : chip sous la jauge + zones de fond dans la courbe.
#
# Code déjà sur disque après `git checkout pi-0.0.26`. db.py impacte l'API ET le
# reader (migration + écriture du tariff) → on redémarre les deux. Pas de reboot.
#
# Tourne en `ben` ; sudo pour les restarts. Idempotent.

set -euo pipefail

TR="pi-0.0.25 → pi-0.0.26"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
DEVICE_JSON="/etc/ben-firmware/device.json"

[ -f "$REPO/src/pi/store/db.py" ] || fail "manquant : src/pi/store/db.py"

MODEL=$(python3 -c "import json;print(json.load(open('$DEVICE_JSON'))['model'])") \
    || fail "lecture du model depuis device.json"
log "[1/2] modèle = $MODEL"

log "[2/2] restart ben-local-api + reader (colonne tariff + /live,/measurements)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
case "$MODEL" in
    pi0-wired)
        sudo systemctl restart ben-tic-reader.service || fail "restart tic-reader" ;;
    pi0-lora)
        sudo systemctl restart ben-lora-receiver.service || fail "restart lora-receiver" ;;
    pi0-lora-wired)
        sudo systemctl restart ben-tic-reader.service ben-lora-receiver.service \
            || fail "restart readers" ;;
    *)
        log "modèle sans reader connu — skip restart reader" ;;
esac

log "✓ update OK — tariff exposé (HC/HP : chip + zones app)"
