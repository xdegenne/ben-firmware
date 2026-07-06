#!/usr/bin/env bash
# update.sh — pi-0.0.54 → pi-0.1.0   (pi0-lora)
#
# Palier 0.1.0 (fin des 0.0.x). Récepteur LoRa : décodeur de trame EXTRAIT dans frame_codec.py
# (module pur, testable — réutilisé par le banc de test) ; main.py délègue à frame_codec.decode.
# Nouveautés portées par le codec : décode IINST 2e courbe (histo) + DÉCHIFFREMENT ChaCha20
# (encrypt-then-MAC, bit7 FLAG_ENC). ALIGNEMENT DU LOGGING avec le lecteur wired : les champs
# collectés-mais-non-stockés (DEMAIN/ADPS/PEJP, NJOURF/NJOURF+1) sont logués INFO ON-CHANGE via
# log_uncabled (même format des deux côtés).
# STRICTEMENT ADDITIF : aucune migration BDD, aucun champ API retiré/renommé. Rétro-compatible :
# frame_codec décode aussi les trames NON chiffrées (émetteur < arduino 0.1.0).
# Code déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.0.54 → pi-0.1.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
test -f "$REPO/src/pi/lora-receiver/frame_codec.py" \
    || fail "frame_codec.py absent (checkout incomplet ?)"
grep -q 'def log_uncabled' "$REPO/src/pi/lora-receiver/main.py" \
    || fail "main.py pas à jour (log_uncabled absent — checkout incomplet ?)"
log "restart ben-lora-receiver (frame_codec + déchiffrement + logging aligné) + ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
sudo systemctl restart ben-local-api.service     || fail "restart ben-local-api"
log "✓ update OK — palier 0.1.0 (frame_codec unifié + chiffrement + logging aligné LoRa/wired)"
