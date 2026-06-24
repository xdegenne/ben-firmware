#!/usr/bin/env bash
# update.sh — pi-0.0.45 → pi-0.0.46   (pi0-lora)
#
# Trame courbe LoRa v0x05 — HORODATAGE PAR POINT (dt). Le récepteur décode désormais des
# paires [dt, delta_papp] : chaque point porte son intervalle réel (secondes, varint) depuis
# le précédent, au lieu d'un period_ds uniforme supposé. Reconstruction t[i] = t0 + Σdt →
# courbe fidèle quelle que soit la cadence (trames ratées incluses). En standard le dt vient
# de l'horodate compteur (meter_ts exact, sans dérive) ; period_ds conservé comme moyenne/hint.
# Compat v0x04 retirée (un seul émetteur en service).
#
# Modif curve_codec.py + libellés main.py. AUCUNE migration BDD (ts/meter_ts déjà présents
# depuis 0.0.43). Code déjà sur disque après `git checkout pi-0.0.46`. Tourne en `ben`.
#
# ⚠️ ORDRE : ce Pi doit être en pi-0.0.46 AVANT que l'Arduino émetteur passe en 0.0.6
#    (un récepteur < 0.0.46 rejette le v0x05 → « Longueur incorrecte : N octets, 20 attendus »).

set -euo pipefail

TR="pi-0.0.45 → pi-0.0.46"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fou : le checkout a bien amené le décodeur v0x05 ?
grep -q "PROTOCOL_VERSION_CURVE = 0x05" "$REPO/src/pi/lora-receiver/curve_codec.py" \
    || fail "curve_codec.py pas à jour (v0x05 absent — checkout incomplet ?)"
grep -q "sample_dt_s" "$REPO/src/pi/lora-receiver/curve_codec.py" \
    || fail "curve_codec.py pas à jour (dt par point absent)"

log "[1/1] restart ben-local-api + ben-lora-receiver (décodage courbe v0x05)"
sudo systemctl restart ben-local-api.service    || fail "restart ben-local-api"
sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"

log "✓ update OK — récepteur courbe v0x05 (horodatage par point, meter_ts exact en standard)"
