#!/usr/bin/env bash
# update.sh — pi-0.8.3 → pi-0.8.4   (MONO-FLUX) : watchdog de SILENCE récepteur + fix on_connect
#
# Deux fixes terrain, embarqués ensemble :
#
#  1. lora-receiver — WATCHDOG DE SILENCE. Le self-test radio existant ne détecte PAS un RX
#     « vivant mais SOURD » : IRQ RX morte alors que le SPI reste lisible (REG_VERSION passe) →
#     réception morte, aucune relance. Incident ben-0010 (17/07) : 55 min de silence sans restart.
#     Fix = timeout de TRAFIC à BACKOFF EXPONENTIEL (5 min → ×2 → plafond 1 h), persisté dans
#     lora-state.json (survit aux restarts/reboots), remis à 0 dès qu'une trame RF revient.
#     Désactivé tant qu'aucune trame n'a jamais été reçue (device neuf / sans émetteur).
#     → concerne les devices `lora` : on redémarre ben-lora-receiver pour charger le nouveau code.
#
#  2. provisioner — GARDE ANTI-DOUBLON on_connect. bluezero appelle on_connect 2× sur la MÊME
#     connexion (Connected puis ServicesResolved ~5 s après, sur un téléphone lent) ; le 2e appel
#     écrasait l'état de la reco couleurs en cours → ÉCHEC d'unboxing (bug MIUI/Xiaomi). Un flag
#     `_connected` ignore le 2e appel.
#     → concerne TOUS les modèles, mais le provisioner ne tourne qu'au provisioning : RIEN à
#       redémarrer, effet au prochain unboxing.
set -euo pipefail
TR="pi-0.8.3 → pi-0.8.4"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fous : les deux fixes doivent être présents dans le checkout.
grep -q 'SILENCE_RESTART_BASE_S' "$REPO/src/pi/lora-receiver/main.py" \
    || fail "watchdog de silence absent de lora-receiver (checkout incomplet ?)"
grep -q '_connected' "$REPO/src/pi/provisioner/main.py" \
    || fail "garde on_connect absente du provisioner (checkout incomplet ?)"
log "provisioner : garde on_connect en place — effet au prochain unboxing (rien à redémarrer)"

# Fix 1 : uniquement les devices avec la capability lora → recharger le récepteur.
if python3 "$REPO/src/pi/capabilities.py" has lora; then
    log "capability lora présente → restart ben-lora-receiver (charge le watchdog de silence)"
    sudo systemctl restart ben-lora-receiver.service || fail "restart ben-lora-receiver"
    log "✓ watchdog de silence chargé"
else
    log "pas de capability lora (wired) → récepteur non concerné (skip)"
fi
log "✓ update OK"
