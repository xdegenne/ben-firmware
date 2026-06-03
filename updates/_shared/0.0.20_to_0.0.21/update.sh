#!/usr/bin/env bash
# update.sh — pi-0.0.20 → pi-0.0.21
#
# NO-OP : la 0.0.21 ne change que le code du BLE provisioner
# (src/pi/provisioner/) — il remonte désormais l'IP locale du device en
# suffixe du statut (`connected:<ip>`) pour que l'app puisse se connecter
# directement sur le LAN après le provisioning (mode proto).
#
# Ce code est déjà en place après le `git checkout pi-0.0.21` fait par
# l'updater. Le service ben-ble-provisioner ne tourne que hors-ligne (au
# boot sans Internet), donc rien à redémarrer ici : la nouvelle logique
# sera utilisée au prochain passage en mode provisioning.

set -euo pipefail
echo "[update pi-0.0.20 → pi-0.0.21] no-op (code provisioner pris au checkout)"
echo "[update pi-0.0.20 → pi-0.0.21] ✓ done"
