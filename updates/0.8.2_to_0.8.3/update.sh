#!/usr/bin/env bash
# update.sh — pi-0.8.2 → pi-0.8.3   (MONO-FLUX) : RETRAIT de l'agent de pairing BLE
#
# CONTEXTE : la 0.8.2 avait ajouté un agent de pairing Just Works au provisioner, en croyant que
# le pairing était la cause du bug iOS. C'ÉTAIT UN LEURRE — le vrai fix iOS était la MTU
# (DEVICE_INFO compact, déjà en 0.8.2). L'agent, lui, faisait BONDER iOS → après un bond, iOS
# re-découvre les services → les handles de caractéristiques de l'app deviennent périmés → GEL des
# couleurs / de la vérif au 1er unboxing ; et au re-provisioning le bond effacé côté device →
# « Peer removed pairing information » côté iOS.
#
# → On RETIRE l'agent. Sans bond : pas de re-découverte, pas de « Peer removed », 1er unboxing OK.
#   iOS tolère l'échec du pairing Service Changed et lit en clair (le fix MTU suffit).
#
# → RIEN à redémarrer : le provisioner ne tourne pas sur un device en service. Effet au prochain
#   provisioning. No-op volontaire.
set -euo pipefail
TR="pi-0.8.2 → pi-0.8.3"
log() { echo "[update $TR] $*"; }

REPO="${REPO_PATH:-/opt/ben/repo}"
# Garde-fou : l'agent doit AVOIR ÉTÉ RETIRÉ du provisioner checké out.
if grep -q 'PairingAgent' "$REPO/src/pi/provisioner/main.py"; then
    echo "[update $TR] ✗ ERREUR : agent encore présent (checkout incomplet ?)" >&2
    exit 1
fi

log "agent de pairing retiré — effet au prochain provisioning"
log "✓ update OK"
