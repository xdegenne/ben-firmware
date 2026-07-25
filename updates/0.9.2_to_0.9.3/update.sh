#!/usr/bin/env bash
# update.sh — → pi-0.9.3   : PAPP/IINST dans le boot (conso dès le 1er boot, unboxing rapide) +
#                            fix blink blanc BLOQUANT (retardait l'ACK applicatif).
#
# 1) UNBOXING RAPIDE : l'émetteur (arduino ≥ 0.1.6) met PAPP + IINST dans la trame de boot. Le
#    récepteur les décode (frame_codec T_PAPP/T_IINST) et insère une mesure IMMÉDIATE dès le boot
#    (ben-telemetry), AVANT tout streaming courbe → /live affiche la conso au 1er boot. Histo :
#    PAPP=0 en injection → IINST porte 230×IINST = production estimée.
# 2) FIX BLINK BLOQUANT : le blink blanc "discovery" (ben-telemetry) faisait sleep(2.0s) sous
#    _led_lock ; le flash RF de on_recv (ben-radio) bloquait alors dessus AVANT send_app_ack →
#    l'ACK partait trop tard → l'émetteur ratait sa fenêtre → boucle de boots. Fix : blanc 2.0→0.2s
#    + flash RF déplacé APRÈS l'ACK.
# Détail : ../../CHANGELOG.md (0.9.3).
#
# Changement PUREMENT code (frame_codec.py + ben_telemetry.py + ben_radio.py) : aucune dépendance,
# aucune migration, aucun changement d'unit → restart ben-radio + ben-telemetry.
# Rétro-compatible (émetteur < 0.1.6 : pas de PAPP/IINST au boot → comportement inchangé).
# Concerne les devices `lora-tic-receiver`. Sur wired pur → skip.
# Code déjà sur disque après `git checkout pi-0.9.3`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.3"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Gate modèle : uniquement les devices qui reçoivent du LoRa TIC ─────────────────────────────
if ! python3 "$REPO/src/pi/capabilities.py" has lora-tic-receiver; then
    log "pas de capability lora-tic-receiver (wired pur) → non concerné (skip)"
    exit 0
fi

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q 'T_PAPP'  "$REPO/src/pi/lora-receiver/frame_codec.py"      || fail "T_PAPP absent de frame_codec.py (checkout pi-0.9.3 incomplet ?)"
grep -q 'T_IINST' "$REPO/src/pi/lora-receiver/frame_codec.py"      || fail "T_IINST absent de frame_codec.py"
grep -q 'measurement instantané' "$REPO/src/pi/ben-telemetry/ben_telemetry.py" || fail "record boot-papp absent de ben_telemetry.py"

# ── Restart des 2 services (ben-telemetry importe frame_codec ; ben-radio = reorder ACK/LED) ───
log "[1/2] restart ben-telemetry (boot PAPP/IINST → measurement + blink 0.2s)"
sudo systemctl restart ben-telemetry.service || fail "restart ben-telemetry"
log "[2/2] restart ben-radio (flash RF déplacé après l'ACK)"
sudo systemctl restart ben-radio.service     || fail "restart ben-radio"
sleep 3
systemctl is-active ben-telemetry.service >/dev/null || fail "ben-telemetry inactif après restart"
systemctl is-active ben-radio.service     >/dev/null || fail "ben-radio inactif après restart"

log "✓ ben-radio + ben-telemetry actifs · PAPP/IINST au boot · blink non-bloquant"
log "✓ update OK"
