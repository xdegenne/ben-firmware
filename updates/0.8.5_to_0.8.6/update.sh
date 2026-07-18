#!/usr/bin/env bash
# update.sh — pi-0.8.5 → pi-0.8.6   (MONO-FLUX) : FIX stockage IINST en histo/LoRa
#
# CONTEXTE : l'émetteur envoie la 2e courbe IINST en historique (flag CF_HAS_IINST) et le
# récepteur la DÉCODE bien (frame_codec → decoded["iinst"]), mais `on_recv_curve` construisait
# ses rows avec PAPP seulement → il OUBLIAIT `decoded["iinst"]` → la colonne `iinst` restait NULL
# pour tout le flux LoRa histo (l'intensité arrivait par les airs puis était jetée au stockage).
# Perdu lors du refactor vers le stockage générique v0x05 index-based ; le banc de test, lui,
# la lisait toujours (d'où « OK au banc, absent en prod »).
#
# FIX (lora-receiver/main.py) : extraire `iinst = decoded.get("iinst")` et ajouter
# `labels["IINST"] = iinst[i]` dans la boucle de stockage. La colonne `iinst` et le mapping
# du label "IINST" existent déjà en base → rien d'autre à toucher. Sans effet en standard
# (papp net signé, pas de 2e courbe IINST).
#
# → concerne les devices `lora` : on redémarre ben-lora-receiver pour charger le nouveau code.
set -euo pipefail
TR="pi-0.8.5 → pi-0.8.6"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q 'labels\["IINST"\] = iinst' "$REPO/src/pi/lora-receiver/main.py" \
    || fail "fix IINST absent de on_recv_curve (checkout incomplet ?)"

if python3 "$REPO/src/pi/capabilities.py" has lora; then
    log "capability lora présente → restart ben-lora-receiver (stockage IINST histo)"
    sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
    log "✓ IINST histo de nouveau stockée en base"
else
    log "pas de capability lora (wired) → récepteur non concerné (skip)"
fi
log "✓ update OK"
