#!/usr/bin/env bash
# update.sh — pi-0.8.6 → pi-0.8.7   (MONO-FLUX) : « au revoir » au désappairage (3 flashs violets)
#
# Au désappairage (`POST /unprovision`), le boîtier oublie le WiFi puis s'éteint (poweroff).
# On ajoute un signal clair de départ : **3 flashs VIOLETS** sur la LED RGB juste avant le
# poweroff. local_api._unprovision stoppe désormais les readers TOUJOURS (avant : seulement si
# wipe) pour (1) libérer les pins GPIO de la LED et (2) fermer la base — puis flashe violet.
#
# → concerne TOUS les modèles : `ben-local-api` (qui gère /unprovision) tourne partout. On le
#   redémarre pour charger le nouveau code. Effet au prochain désappairage.
set -euo pipefail
TR="pi-0.8.6 → pi-0.8.7"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

grep -q '_goodbye_flash' "$REPO/src/pi/store/local_api.py" \
    || fail "flash « au revoir » absent de local_api (checkout incomplet ?)"

if systemctl is-active --quiet ben-local-api.service; then
    log "restart ben-local-api (charge le flash « au revoir » du désappairage)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    log "✓ /unprovision fera 3 flashs violets avant l'extinction"
else
    log "ben-local-api inactif (device non provisionné ?) — rien à redémarrer"
fi
log "✓ update OK"
