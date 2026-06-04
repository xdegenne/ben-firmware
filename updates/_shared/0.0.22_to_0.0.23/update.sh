#!/usr/bin/env bash
# update.sh — pi-0.0.22 → pi-0.0.23
#
# 1) Réglages utilisateur via l'API locale : luminosité LED par crans (led_level
#    0..5, 0 = éteinte), mapping perceptuel (gamma). settings.py + GET/POST
#    /settings ; la couche LED applique le cran. bypass=True (décidé par
#    l'appelant) pour erreurs + provisioning → toujours visibles, même éteint.
# 2) RATTRAPAGE de l'annonce mDNS `_ben._tcp` (avahi) : elle avait raté la 0.0.22
#    publiée (commit amendé après push) → on l'installe ici pour que l'app
#    puisse découvrir le boîtier sur le LAN.
#
# Le nouveau code est déjà sur disque après le `git checkout pi-0.0.23`. On
# installe le service avahi (idempotent), puis on redémarre l'API + le reader
# pour charger le code. Le provisioner (static, hors-ligne) le prendra à son
# prochain lancement. Résilient : aucun settings.json à créer (defaults en code).
#
# Tourne en `ben` ; sudo pour les opérations privilégiées. Pas de reboot.

set -euo pipefail

TR="pi-0.0.22 → pi-0.0.23"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
DEVICE_JSON="/etc/ben-firmware/device.json"

MODEL=$(python3 -c "import json;print(json.load(open('$DEVICE_JSON'))['model'])") \
    || fail "lecture du model depuis device.json"
log "[1/3] modèle = $MODEL"

# Rattrapage annonce mDNS _ben._tcp (manquante en 0.0.22)
AVAHI_SRC="$REPO/config/avahi/ben.service"
[ -f "$AVAHI_SRC" ] || fail "fichier avahi manquant : $AVAHI_SRC"
log "[2/3] install annonce mDNS _ben._tcp (:8087)"
sudo install -m 644 -o root -g root "$AVAHI_SRC" /etc/avahi/services/ben.service \
    || fail "install avahi service"
sudo systemctl reload-or-restart avahi-daemon 2>/dev/null || true

log "[3/3] restart ben-local-api + reader (chargement des réglages LED)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
case "$MODEL" in
    pi0-wired)       sudo systemctl restart ben-tic-reader.service || fail "restart tic-reader" ;;
    pi0-lora)        sudo systemctl restart ben-lora-receiver.service || fail "restart lora-receiver" ;;
    pi0-lora-wired)  sudo systemctl restart ben-tic-reader.service || fail "restart tic-reader" ;;
    *)               log "modèle sans reader connu — skip restart reader" ;;
esac

log "✓ update OK — luminosité LED réglable (POST /settings) + découverte mDNS"
