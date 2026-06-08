#!/usr/bin/env bash
# update.sh — pi-0.0.37 → pi-0.0.38
#
# Provisioning BLE : phase d'APPRENTISSAGE des couleurs avant le test.
#  - led.py : la boucle de séquence peut notifier la couleur affichée (callback
#    on_show + tokens) → synchro avec l'app.
#  - provisioner/main.py : 2 nouvelles caractéristiques GATT
#      PREVIEW_CMD   (…0007, write "1"/"0")
#      PREVIEW_COLOR (…0008, read|notify : B|Y|W|R|-)
#    "1" joue les 4 couleurs en boucle (ordre fixe BYWR) + notifie la couleur ;
#    "0" arrête et enchaîne sur l'affichage du code de test.
#  - Durée d'affichage de chaque couleur du CODE de test allongée (1.3 s).
#
# Le provisioner est ON-DEMAND (ne tourne qu'en mode BLE, lancé par
# check_network) → RIEN à redémarrer : le nouveau code est pris au prochain
# passage en provisioning BLE (désappairage). Pas de reboot.
#
# Code sur disque après `git checkout pi-0.0.38`. Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.37 → pi-0.0.38"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q "PREVIEW_CMD_UUID" "$REPO/src/pi/provisioner/main.py" \
    || fail "main.py pas à jour (checkout incomplet ?)"
grep -q "on_show" "$REPO/src/pi/provisioner/led.py" \
    || fail "led.py pas à jour (checkout incomplet ?)"

log "✓ update OK — apprentissage couleurs (PREVIEW) + code de test plus long ;"
log "  provisioner on-demand → rien à redémarrer (pris au prochain mode BLE)."
